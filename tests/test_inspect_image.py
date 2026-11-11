"""Offline tests for inspect_image and its session-history integration.

inspect_image attaches the image to the current conversation instead of
running a separate vision request. These tests cover the tool's
pipeline (pod copy, downscale, token cost), the pre-flight modality
check, the reserved `image` key handling in the tool executor, the
history message the session builds, the token estimator, elision
safety with list content, the compaction transcript placeholder, and
the dump redaction. No pod, no network: every boundary is faked.
"""

import json
import math
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite
from PIL import Image

import syna.conversation.session as session_module
import syna.protocol.parse as parse_module
from syna.ask_utils import _write_dump, ask_question
from syna.capabilities import vision
from syna.conversation.session import AgentSession
from syna.protocol.channel import is_channel_message, unwrap_channel
from syna.protocol.compaction import build_transcript
from syna.protocol.context import (
    elide_archived_user_messages,
    elide_tool_outputs,
    estimate_history_tokens,
)
from syna.protocol.images import image_token_cost, redact_image_data


def _png_bytes(width: int, height: int) -> bytes:
    """Real PNG bytes of a solid-color image."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _configure_vision(monkeypatch, *, pod_bytes=None, model="vision/model"):
    """Point vision.py at fakes; return the recorded pod copies."""
    copies = []

    def fake_copy_from(container, source, destination):
        copies.append(source)
        if pod_bytes is not None:
            name = source.rsplit("/", 1)[-1]
            Path(destination, name).write_bytes(pod_bytes)

    monkeypatch.setattr(vision, "get_env", lambda: {"MODEL": model})
    monkeypatch.setattr(vision, "get_container", lambda: object())
    monkeypatch.setattr(vision, "pod_copy_from", fake_copy_from)
    monkeypatch.setattr(session_module, "model_accepts_images", lambda m: True)
    return copies


def test_inspect_image_attaches_downscaled_png(monkeypatch):
    _configure_vision(monkeypatch, pod_bytes=_png_bytes(2000, 1000))

    result = vision.inspect_image("/workspace/render.png")

    assert "note" in result and "image" in result
    image = result["image"]
    assert image["path"] == "/workspace/render.png"
    assert (image["width"], image["height"]) == (1024, 512), (
        "the 1024x1024 box caps the larger side, aspect preserved"
    )
    assert image["data_url"].startswith("data:image/png;base64,")
    assert image["image_tokens"] == math.ceil(1024 * 512 / 750)


def test_inspect_image_small_image_keeps_size(monkeypatch):
    _configure_vision(monkeypatch, pod_bytes=_png_bytes(100, 50))

    image = vision.inspect_image("/workspace/icon.png")["image"]

    assert (image["width"], image["height"]) == (100, 50)
    assert image["image_tokens"] == image_token_cost(100, 50)


def test_inspect_image_rejects_non_image(monkeypatch):
    _configure_vision(monkeypatch, pod_bytes=b"not an image")

    result = vision.inspect_image("/workspace/data.txt")

    assert "Could not read" in result["error"]


def test_inspect_image_blocks_text_only_models(monkeypatch):
    copies = _configure_vision(monkeypatch, pod_bytes=_png_bytes(10, 10))
    monkeypatch.setattr(session_module, "model_accepts_images", lambda m: False)

    result = vision.inspect_image("/workspace/render.png")

    assert "does not accept image input" in result["error"]
    assert copies == [], "the pod is not touched when the model cannot see"


def test_inspect_image_proceeds_when_support_unknown(monkeypatch):
    _configure_vision(monkeypatch, pod_bytes=_png_bytes(10, 10))
    monkeypatch.setattr(session_module, "model_accepts_images", lambda m: None)

    result = vision.inspect_image("/workspace/render.png")

    assert "image" in result, "unknown support proceeds optimistically"


def test_inspect_image_without_model_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(vision, "get_env", dict)
    monkeypatch.setattr(vision, "DEFAULT_MODEL", "")

    result = vision.inspect_image("/workspace/render.png")

    assert "MODEL is not set" in result["error"]


def test_model_accepts_images_lookup(monkeypatch):
    monkeypatch.setattr(
        session_module,
        "_image_support_cache",
        {"vision/model": True, "text/model": False},
    )
    assert session_module.model_accepts_images("vision/model") is True
    assert session_module.model_accepts_images("text/model") is False
    assert session_module.model_accepts_images("~vision/model") is True, (
        "tilde-prefixed ids match the listed form"
    )
    assert session_module.model_accepts_images("unlisted/model") is None
    monkeypatch.setattr(session_module, "_image_support_cache", None)
    assert session_module.model_accepts_images("vision/model") is None, (
        "a pending or failed fetch is unknown, never a block"
    )


def _fake_inspect_payload(file_path: str):
    """Stand-in inspect_image returning the reserved image payload."""
    return {
        "note": "Image attached to the conversation (100x50); you can see it now.",
        "image": {
            "path": file_path,
            "width": 100,
            "height": 50,
            "data_url": "data:image/png;base64,QUJD",
            "image_tokens": 7,
        },
    }


def _run_inspect_turn(monkeypatch, file_paths: list[str]) -> AgentSession:
    """Run one turn whose reply calls inspect_image once per path."""
    monkeypatch.setattr(
        session_module,
        "load_config",
        lambda: {
            "timeout_ms": 120000,
            "retry": {
                "strategy": "backoff",
                "initial_interval_ms": 1000,
                "max_interval_ms": 10000,
                "exponent": 1.5,
                "max_elapsed_time_ms": 30000,
                "retry_connection_errors": False,
            },
        },
    )
    monkeypatch.setattr(
        session_module,
        "get_env",
        lambda: {"MODEL": "vision/model", "OPENROUTER_API_KEY": "sk-or-test"},
    )
    monkeypatch.setitem(
        parse_module.TOOL_REGISTRY, "inspect_image", _fake_inspect_payload
    )

    replies = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": "inspect_image",
                        "arguments": json.dumps({"file_path": path}),
                    },
                }
                for index, path in enumerate(file_paths)
            ],
        },
        {"role": "assistant", "content": "I can see the image."},
    ]

    def fake_ask_question(client, model, messages, **kwargs):
        reply = replies.pop(0)
        messages.append(reply)
        return reply

    monkeypatch.setattr(session_module, "ask_question", fake_ask_question)
    session = AgentSession(session_id="inspect-test")
    session.run_turn("look at this")
    return session


def test_run_turn_attaches_image_after_tool_results(monkeypatch):
    session = _run_inspect_turn(monkeypatch, ["/workspace/a.png", "/workspace/b.png"])

    roles = [m.get("role") for m in session.messages]
    assert roles == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
        "user",
        "assistant",
    ], "image messages follow the whole contiguous tool group"
    first_image = session.messages[5]
    assert isinstance(first_image["content"], list)
    text_part, image_part = first_image["content"]
    assert text_part["type"] == "text"
    assert is_channel_message(text_part["text"], "HARNESS_NOTICE"), (
        "the note is a harness notice, not the user's voice"
    )
    assert "/workspace/a.png" in text_part["text"]
    assert image_part == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,QUJD"},
    }
    assert first_image["image_tokens"] == 7
    assert first_image["image_path"] == "/workspace/a.png"
    assert session.messages[6]["image_path"] == "/workspace/b.png"
    tool_notes = [session.messages[3]["content"], session.messages[4]["content"]]
    assert all("you can see it now" in note for note in tool_notes)
    assert all("base64" not in note for note in tool_notes), (
        "the result text never carries the payload"
    )


def test_image_events_carry_no_base64(monkeypatch):
    events = []
    monkeypatch.setitem(
        parse_module.TOOL_REGISTRY, "inspect_image", _fake_inspect_payload
    )
    outcome = parse_module.parse_and_execute_tools(
        [
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "inspect_image",
                    "arguments": json.dumps({"file_path": "/workspace/a.png"}),
                },
            }
        ],
        on_tool_event=lambda *a, **k: events.append(k),
        echo_errors=False,
    )
    entry = outcome.outputs[0]
    assert entry["image"]["path"] == "/workspace/a.png"
    assert "base64" not in entry["output"]
    for event_kwargs in events:
        structured = event_kwargs.get("structured_output")
        assert structured is None or "data_url" not in json.dumps(structured), (
            "the UI event log must never receive the image payload"
        )


def test_plain_error_dicts_still_flow_through(monkeypatch):
    def failing(file_path: str):
        return {"error": "Could not read it as an image"}

    monkeypatch.setitem(parse_module.TOOL_REGISTRY, "inspect_image", failing)
    outcome = parse_module.parse_and_execute_tools(
        [
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "inspect_image",
                    "arguments": json.dumps({"file_path": "/workspace/x.png"}),
                },
            }
        ],
        echo_errors=False,
    )
    entry = outcome.outputs[0]
    assert "image" not in entry
    assert "Could not read" in entry["output"]


def _image_message(path="/workspace/a.png", tokens=7):
    return session_module._image_history_message(
        {
            "path": path,
            "width": 100,
            "height": 50,
            "data_url": "data:image/png;base64,QUJD",
            "image_tokens": tokens,
        }
    )


def test_estimator_counts_image_tokens_not_base64():
    messages = [
        {"role": "user", "content": "hi"},
        _image_message(tokens=700),
    ]
    total = estimate_history_tokens(messages)
    assert 700 <= total <= 800, (
        "the recorded estimate counts; the base64 characters do not"
    )


def test_elision_leaves_image_messages_untouched():
    image_message = _image_message()
    messages = [{"role": "system", "content": "sys"}, image_message]
    assert elide_tool_outputs(messages, keep_recent=0) == 0
    assert elide_archived_user_messages(messages, 0, 100, "nonce") == 0
    assert isinstance(image_message["content"], list), "no crash, no rewrite"


def test_first_user_index_ignores_image_messages():
    history = [
        _image_message(),
        {"role": "user", "content": "the real first request"},
    ]
    assert AgentSession._first_user_index(history) == 1


def test_compaction_transcript_uses_placeholders():
    transcript = build_transcript(
        [
            {"role": "user", "content": "check my chart"},
            _image_message(),
        ]
    )
    assert "[image: /workspace/a.png]" in transcript
    assert "base64" not in transcript
    kind, _body = unwrap_channel(
        session_module.content_text(_image_message()["content"])
    ) or (None, None)
    assert kind == "HARNESS_NOTICE"


def test_dump_redaction():
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "note"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + "A" * 5000},
                    },
                ],
            }
        ]
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        dump = Path(temp_dir) / "dump.json"
        _write_dump(dump, payload)
        written = dump.read_text(encoding="utf-8")
    assert "A" * 100 not in written
    assert "[image data redacted:" in written


def test_redact_image_data_leaves_other_text():
    text = "plain text with data:text/plain;base64,QUJD untouched"
    assert redact_image_data(text) == text
    redacted = redact_image_data("x data:image/png;base64,QUJD y")
    assert "QUJD" not in redacted and "[image data redacted:" in redacted


def test_wire_payload_strips_internal_keys(monkeypatch):
    """Harness bookkeeping on messages must never reach the provider."""
    captured = []

    class FakeChat:
        def send(self, **request):
            captured.append(request)
            message = SimpleNamespace(content="done", refusal=None, tool_calls=None)
            choice = SimpleNamespace(finish_reason="stop", message=message)
            return SimpleNamespace(choices=[choice], usage=None)

    import syna.ask_utils as ask_utils_module

    monkeypatch.setattr(ask_utils_module, "load_config", dict)
    client = SimpleNamespace(chat=FakeChat())
    messages = [
        {"role": "system", "content": "sys"},
        _image_message(),
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        dump = Path(temp_dir) / "dump.json"
        reply = ask_question(client, "vision/model", messages, delay=0, dump_path=dump)
        written = dump.read_text(encoding="utf-8")

    wire = captured[0]["messages"]
    assert all(set(m) <= {"role", "content"} for m in wire), (
        "image_tokens and image_path stay out of the request"
    )
    assert "image_tokens" not in json.dumps(captured[0])
    assert messages[1].get("image_tokens") == 7, (
        "the live history keeps its bookkeeping untouched"
    )
    assert reply["content"] == "done"
    assert "done" in written, "the post-response dump still captures the reply"


def main():
    """Run every inspect_image test and report a summary."""
    run_suite(
        [
            test_inspect_image_attaches_downscaled_png,
            test_inspect_image_small_image_keeps_size,
            test_inspect_image_rejects_non_image,
            test_inspect_image_blocks_text_only_models,
            test_inspect_image_proceeds_when_support_unknown,
            test_inspect_image_without_model_is_a_clean_error,
            test_model_accepts_images_lookup,
            test_run_turn_attaches_image_after_tool_results,
            test_image_events_carry_no_base64,
            test_plain_error_dicts_still_flow_through,
            test_estimator_counts_image_tokens_not_base64,
            test_elision_leaves_image_messages_untouched,
            test_first_user_index_ignores_image_messages,
            test_compaction_transcript_uses_placeholders,
            test_dump_redaction,
            test_redact_image_data_leaves_other_text,
            test_wire_payload_strips_internal_keys,
        ]
    )


if __name__ == "__main__":
    main()
