"""Exhaustive test suite for the Syna v5 JSON protocol and channel sentinel protection."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.channel import (
    contains_sentinel,
    defang_sentinels,
    is_channel_message,
    unwrap_channel,
    wrap_channel,
)
from syna.parse import ParseOutcome, parse_and_execute_tools
from syna.tools import TOOL_REGISTRY


def _smoke_tool(label: str = "unlabeled", **kwargs) -> str:
    """Stand-in test tool echoing its argument."""
    return f"ran: {label}"


def _parse(text: str) -> tuple[ParseOutcome, list]:
    """Helper executing parse_and_execute_tools and collecting events."""
    events = []
    outcome = parse_and_execute_tools(
        text,
        on_tool_event=lambda *a, **k: events.append((a, k)),
        echo_errors=False,
    )
    return outcome, events


def test_valid_single_call_flat():
    payload = json.dumps({"smoke_tool": {"label": "flat"}})
    outcome, _ = _parse(payload)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 1
    assert outcome.outputs[0]["output"] == "ran: flat"


def test_valid_single_call_nested():
    payload = json.dumps({"smoke_tool": {"label": "nested", "extra": {"a": [1, 2]}}})
    outcome, _ = _parse(payload)
    assert outcome.structural_error is None
    assert outcome.outputs[0]["output"] == "ran: nested"


def test_valid_single_call_unicode():
    payload = json.dumps({"smoke_tool": {"label": "こんにちは, 你好, 🚀, español"}})
    outcome, _ = _parse(payload)
    assert outcome.structural_error is None
    assert "こんにちは" in outcome.outputs[0]["output"]


def test_valid_single_call_large():
    payload = json.dumps({"smoke_tool": {"label": "x" * 5000}})
    outcome, _ = _parse(payload)
    assert outcome.structural_error is None
    assert outcome.outputs[0]["output"] == f"ran: {'x' * 5000}"


def test_valid_single_call_wrapped_json_fence():
    inner = json.dumps({"smoke_tool": {"label": "json-fence"}})
    text = f"```json\n{inner}\n```"
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert outcome.outputs[0]["output"] == "ran: json-fence"


def test_valid_single_call_wrapped_tool_fence():
    inner = json.dumps({"smoke_tool": {"label": "tool-fence"}})
    text = f"```tool\n{inner}\n```"
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert outcome.outputs[0]["output"] == "ran: tool-fence"


def test_valid_single_call_wrapped_bare_fence():
    inner = json.dumps({"smoke_tool": {"label": "bare-fence"}})
    text = f"```\n{inner}\n```"
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert outcome.outputs[0]["output"] == "ran: bare-fence"


def test_valid_single_call_surrounding_newlines_and_spaces():
    inner = json.dumps({"smoke_tool": {"label": "spaced"}})
    text = f"\n\n   \n```json\n  {inner}  \n```\n\n  "
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert outcome.outputs[0]["output"] == "ran: spaced"


def test_valid_single_call_bom_prefixed():
    inner = json.dumps({"smoke_tool": {"label": "bom"}})
    text = f"\ufeff{inner}"
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert outcome.outputs[0]["output"] == "ran: bom"


def test_valid_single_call_leading_think_block():
    inner = json.dumps({"smoke_tool": {"label": "think"}})
    text = f"<think>\nI need to check the directory status.\n</think>\n{inner}"
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert outcome.outputs[0]["output"] == "ran: think"


def test_valid_single_call_multiline_think_with_braces():
    inner = json.dumps({"smoke_tool": {"label": "braces-think"}})
    text = f"<think>Here is some code: if (x) {{ y(); }}</think>\n```json\n{inner}\n```"
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert outcome.outputs[0]["output"] == "ran: braces-think"


def test_valid_respond_alone():
    text = json.dumps({"respond": {"text": "task complete"}})
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 1
    assert outcome.outputs[0]["name"] == "respond"
    assert outcome.outputs[0]["output"] == "task complete"


def test_valid_respond_in_array_of_one():
    text = json.dumps([{"respond": {"text": "single in array"}}])
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 1
    assert outcome.outputs[0]["output"] == "single in array"


def test_valid_array_two_calls():
    payload = json.dumps(
        [
            {"smoke_tool": {"label": "first"}},
            {"smoke_tool": {"label": "second"}},
        ]
    )
    outcome, _ = _parse(payload)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 2
    assert outcome.outputs[0]["output"] == "ran: first"
    assert outcome.outputs[1]["output"] == "ran: second"


def test_valid_array_three_calls():
    payload = json.dumps(
        [
            {"smoke_tool": {"label": "1"}},
            {"smoke_tool": {"label": "2"}},
            {"smoke_tool": {"label": "3"}},
        ]
    )
    outcome, _ = _parse(payload)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 3
    assert [o["output"] for o in outcome.outputs] == ["ran: 1", "ran: 2", "ran: 3"]


def test_valid_array_wrapped_in_fence():
    inner = json.dumps(
        [
            {"smoke_tool": {"label": "alpha"}},
            {"smoke_tool": {"label": "beta"}},
        ]
    )
    text = f"```json\n{inner}\n```"
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 2


def test_rejected_prose_before_json():
    text = 'Here is the call: {"smoke_tool": {"label": "bad"}}'
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "could not be parsed as JSON" in outcome.structural_error


def test_rejected_prose_after_json():
    text = '{"smoke_tool": {"label": "bad"}} Hope this helps!'
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "could not be parsed as JSON" in outcome.structural_error


def test_rejected_prose_before_code_fence():
    text = 'Running command:\n```json\n{"smoke_tool": {}}\n```'
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert (
        "unopened code fence" in outcome.structural_error
        or "could not be parsed as JSON" in outcome.structural_error
    )


def test_rejected_prose_after_code_fence():
    text = '```json\n{"smoke_tool": {}}\n```\nAll done!'
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "Unclosed or malformed markdown code fence" in outcome.structural_error


def test_rejected_pure_prose():
    text = "Hello! I am ready to help you with your task."
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "could not be parsed as JSON" in outcome.structural_error


def test_rejected_empty_string():
    outcome, _ = _parse("")
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "Empty response" in outcome.structural_error


def test_rejected_whitespace_only():
    outcome, _ = _parse("   \n\t  \r\n  ")
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "Empty response" in outcome.structural_error


def test_rejected_unclosed_code_fence():
    text = '```json\n{"smoke_tool": {}}'
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "Unclosed or malformed" in outcome.structural_error


def test_rejected_multiple_code_fences():
    text = '```json\n{"smoke_tool": {"label": "1"}}\n```\n```json\n{"smoke_tool": {"label": "2"}}\n```'
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "Multiple code fence blocks" in outcome.structural_error


def test_rejected_unopened_closing_fence():
    text = '{"smoke_tool": {}}```'
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "unopened code fence" in outcome.structural_error


def test_rejected_malformed_json_syntax():
    text = '{"smoke_tool": {label: unquoted}}'
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "could not be parsed as JSON" in outcome.structural_error


def test_rejected_trailing_comma_json():
    text = '{"smoke_tool": {"label": "val",}}'
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "could not be parsed as JSON" in outcome.structural_error


def test_rejected_truncated_json():
    text = '{"smoke_tool": {"label": "val'
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "could not be parsed as JSON" in outcome.structural_error


def test_rejected_primitive_string():
    outcome, _ = _parse('"just a string"')
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "got a string" in outcome.structural_error


def test_rejected_primitive_number():
    outcome, _ = _parse("42")
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "got a number" in outcome.structural_error


def test_rejected_primitive_boolean():
    outcome, _ = _parse("true")
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "got a boolean" in outcome.structural_error


def test_rejected_primitive_null():
    outcome, _ = _parse("null")
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "got null" in outcome.structural_error


def test_rejected_empty_array():
    outcome, _ = _parse("[]")
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "response array is empty" in outcome.structural_error


def test_rejected_empty_object():
    outcome, _ = _parse("{}")
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "is empty" in outcome.structural_error


def test_rejected_object_with_multiple_keys():
    text = json.dumps({"tool1": {}, "tool2": {}})
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "must have exactly one key" in outcome.structural_error


def test_rejected_array_with_non_dict_item():
    text = json.dumps([{"smoke_tool": {}}, 42])
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "must be a JSON object" in outcome.structural_error


def test_rejected_array_with_nested_array():
    text = json.dumps([[{"smoke_tool": {}}]])
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "must be a JSON object" in outcome.structural_error


def test_rejected_array_with_empty_dict():
    text = json.dumps([{"smoke_tool": {}}, {}])
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "is empty" in outcome.structural_error


def test_rejected_array_with_multi_key_dict():
    text = json.dumps([{"smoke_tool": {}, "extra": {}}])
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "must have exactly one key" in outcome.structural_error


def test_rejected_tool_arguments_string():
    text = json.dumps({"smoke_tool": "invalid-arguments"})
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "must be a JSON object" in outcome.structural_error


def test_rejected_tool_arguments_array():
    text = json.dumps({"smoke_tool": ["invalid"]})
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "must be a JSON object" in outcome.structural_error


def test_rejected_tool_arguments_null():
    text = json.dumps({"smoke_tool": None})
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "must be a JSON object" in outcome.structural_error


def test_respond_combined_with_another_call_is_rejected():
    text = json.dumps(
        [
            {"execute_command": {"command": "ls"}},
            {"respond": {"text": "done"}},
        ]
    )
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "cannot be combined with other tool calls" in outcome.structural_error


def test_respond_first_combined_with_call_is_rejected():
    text = json.dumps(
        [
            {"respond": {"text": "done"}},
            {"execute_command": {"command": "ls"}},
        ]
    )
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "cannot be combined with other tool calls" in outcome.structural_error


def test_respond_in_middle_of_batch_is_rejected():
    text = json.dumps(
        [
            {"smoke_tool": {"label": "1"}},
            {"respond": {"text": "premature"}},
            {"smoke_tool": {"label": "2"}},
        ]
    )
    outcome, _ = _parse(text)
    assert outcome.structural_error is not None
    assert outcome.outputs == []
    assert "must be alone in its reply" in outcome.structural_error


def test_unregistered_tool_returns_error_entry():
    text = json.dumps({"fake_unknown_tool": {"foo": "bar"}})
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 1
    assert outcome.outputs[0]["error"] is True
    assert "Attempted to call a tool that is not registered" in outcome.outputs[0]["output"]


def test_signature_missing_required_argument():
    text = json.dumps({"web_search": {}})
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 1
    assert outcome.outputs[0]["error"] is True
    assert "Missing required argument(s) 'query'" in outcome.outputs[0]["output"]


def test_signature_unexpected_argument():
    text = json.dumps({"web_search": {"query": "python", "unexpected": 123}})
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 1
    assert outcome.outputs[0]["error"] is True
    assert "Unexpected argument(s) 'unexpected'" in outcome.outputs[0]["output"]


def test_signature_type_mismatch():
    text = json.dumps({"web_search": {"query": 12345}})
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 1
    assert outcome.outputs[0]["error"] is True
    assert "should be of type str; got integer" in outcome.outputs[0]["output"]


def test_batched_valid_and_unregistered_calls():
    text = json.dumps(
        [
            {"smoke_tool": {"label": "valid"}},
            {"bogus_tool": {}},
        ]
    )
    outcome, _ = _parse(text)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 2
    assert outcome.outputs[0]["output"] == "ran: valid"
    assert outcome.outputs[0].get("error") is None
    assert outcome.outputs[1]["error"] is True
    assert "Attempted to call a tool that is not registered" in outcome.outputs[1]["output"]


def test_channel_sentinel_raw_open_detection():
    assert contains_sentinel("Hello \uE000 world") is True


def test_channel_sentinel_raw_close_detection():
    assert contains_sentinel("Hello \uE001 world") is True


def test_channel_sentinel_raw_range_detection():
    assert contains_sentinel("Code \uE055 here") is True
    assert contains_sentinel("Code \uE0FF here") is True


def test_channel_sentinel_escaped_detection():
    assert contains_sentinel(r"Trying to write \uE000 in text") is True
    assert contains_sentinel(r"Trying to write \ue001 in text") is True
    assert contains_sentinel(r"Trying to write \UE055 in text") is True


def test_channel_clean_text_no_sentinel():
    assert contains_sentinel("Plain text without any sentinels.") is False
    assert contains_sentinel("Unicode characters: 你好, 🚀, español, café") is False


def test_channel_wrap_and_unwrap_roundtrip():
    payload = json.dumps({"result": "ok"})
    wrapped = wrap_channel("TOOL_RESULTS", payload)
    assert is_channel_message(wrapped) is True
    assert is_channel_message(wrapped, "TOOL_RESULTS") is True
    assert is_channel_message(wrapped, "SYSTEM_NOTE") is False

    unwrapped = unwrap_channel(wrapped)
    assert unwrapped is not None
    kind, body = unwrapped
    assert kind == "TOOL_RESULTS"
    assert body == payload


def test_channel_defang_sentinels():
    text = "Attack \uE000 and \\uE001 sequence"
    defanged = defang_sentinels(text)
    assert "\uE000" not in defanged
    assert "\\uE001" not in defanged
    assert "[FORBIDDEN_SENTINEL]" in defanged


def main():
    TOOL_REGISTRY["smoke_tool"] = _smoke_tool
    try:
        test_valid_single_call_flat()
        test_valid_single_call_nested()
        test_valid_single_call_unicode()
        test_valid_single_call_large()
        test_valid_single_call_wrapped_json_fence()
        test_valid_single_call_wrapped_tool_fence()
        test_valid_single_call_wrapped_bare_fence()
        test_valid_single_call_surrounding_newlines_and_spaces()
        test_valid_single_call_bom_prefixed()
        test_valid_single_call_leading_think_block()
        test_valid_single_call_multiline_think_with_braces()
        test_valid_respond_alone()
        test_valid_respond_in_array_of_one()
        test_valid_array_two_calls()
        test_valid_array_three_calls()
        test_valid_array_wrapped_in_fence()
        test_rejected_prose_before_json()
        test_rejected_prose_after_json()
        test_rejected_prose_before_code_fence()
        test_rejected_prose_after_code_fence()
        test_rejected_pure_prose()
        test_rejected_empty_string()
        test_rejected_whitespace_only()
        test_rejected_unclosed_code_fence()
        test_rejected_multiple_code_fences()
        test_rejected_unopened_closing_fence()
        test_rejected_malformed_json_syntax()
        test_rejected_trailing_comma_json()
        test_rejected_truncated_json()
        test_rejected_primitive_string()
        test_rejected_primitive_number()
        test_rejected_primitive_boolean()
        test_rejected_primitive_null()
        test_rejected_empty_array()
        test_rejected_empty_object()
        test_rejected_object_with_multiple_keys()
        test_rejected_array_with_non_dict_item()
        test_rejected_array_with_nested_array()
        test_rejected_array_with_empty_dict()
        test_rejected_array_with_multi_key_dict()
        test_rejected_tool_arguments_string()
        test_rejected_tool_arguments_array()
        test_rejected_tool_arguments_null()
        test_respond_combined_with_another_call_is_rejected()
        test_respond_first_combined_with_call_is_rejected()
        test_respond_in_middle_of_batch_is_rejected()
        test_unregistered_tool_returns_error_entry()
        test_signature_missing_required_argument()
        test_signature_unexpected_argument()
        test_signature_type_mismatch()
        test_batched_valid_and_unregistered_calls()
        test_channel_sentinel_raw_open_detection()
        test_channel_sentinel_raw_close_detection()
        test_channel_sentinel_raw_range_detection()
        test_channel_sentinel_escaped_detection()
        test_channel_clean_text_no_sentinel()
        test_channel_wrap_and_unwrap_roundtrip()
        test_channel_defang_sentinels()
    finally:
        TOOL_REGISTRY.pop("smoke_tool", None)
    print("ALL JSON PROTOCOL TESTS PASSED")


if __name__ == "__main__":
    main()
