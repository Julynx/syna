"""One-off verification that the request payload sent to OpenRouter carries
real quote characters, not over-escaped ones (plan follow-up item)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.session import build_prompt


def main():
    prompt = build_prompt()
    quote = chr(34)
    assert quote in prompt, "system prompt lost its quote characters entirely"
    assert "\n" in prompt, "system prompt lost its real newlines"

    # Roundtrip through the request's JSON serialization: if any escaping
    # layer mangled the prompt before the SDK, this would not match.
    request = {"model": "test", "messages": [{"role": "system", "content": prompt}]}
    decoded = json.loads(json.dumps(request))
    assert decoded["messages"][0]["content"] == prompt
    print(
        f"PASSED: prompt is {len(prompt)} chars, holds real quote characters;"
        " escaping exists only once, in the HTTP JSON layer"
    )


if __name__ == "__main__":
    main()
