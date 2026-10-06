"""Transcript rendering and request assembly for history compaction."""

from pathlib import Path

from .config import get_project_root

COMPACTION_NOTE_PREFIX = "[System]: [Compaction] "


def load_compaction_prompt() -> str:
    """Load the compaction system prompt from the assets folder."""
    prompt_path = get_project_root() / "assets" / "compaction_prompt.md"
    return prompt_path.read_text(encoding="utf-8")


def is_summary_message(content: str) -> bool:
    """Return whether a message content is a compaction summary."""
    return content.startswith(COMPACTION_NOTE_PREFIX)


def build_transcript(messages: list[dict]) -> str:
    """Render history messages as labeled transcript blocks.

    Compaction summaries are stripped of their wrapper and rendered under a
    dedicated label so a new compaction merges them instead of treating them
    as fresh transcript.
    """
    blocks = []
    for message in messages:
        content = message["content"]
        if is_summary_message(content):
            summary = content[len(COMPACTION_NOTE_PREFIX):].strip()
            blocks.append(f"[previous summary]:\n{summary}")
        else:
            blocks.append(f"[{message['role']}]:\n{content}")
    return "\n\n".join(blocks)


def build_compaction_messages(
    system_prompt: str, messages: list[dict]
) -> list[dict]:
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
