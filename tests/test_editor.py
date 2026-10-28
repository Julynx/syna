"""Tests for the line editor's operation validation and clean error messages."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.capabilities.editor import edit_file

SAMPLE = "line one\nline two\nline three\n"


def _make_file():
    path = Path(tempfile.mkdtemp(prefix="syna-editor-test-"), "sample.txt")
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def _expect_value_error(operations, fragment):
    path = _make_file()
    try:
        edit_file(path, operations)
    except ValueError as exc:
        assert fragment in str(exc), str(exc)
    else:
        raise AssertionError(f"expected ValueError for {operations!r}")
    assert path.read_text(encoding="utf-8") == SAMPLE


def test_valid_edit_still_applies():
    path = _make_file()
    edit_file(
        path,
        [
            {
                "replace": {
                    "line_num_or_range": [2, 2],
                    "replace_with_lines": ["LINE TWO"],
                }
            }
        ],
    )
    assert path.read_text(encoding="utf-8") == "line one\nLINE TWO\nline three\n"
    print("PASS valid edit applies")


def test_valid_combined_operations_still_apply():
    path = _make_file()
    edit_file(
        path,
        [
            {"insert_above": {"line_num": 1, "insert_lines": ["header"]}},
            {
                "replace": {
                    "line_num_or_range": [3, 3],
                    "replace_with_lines": ["LINE THREE"],
                }
            },
        ],
    )
    assert (
        path.read_text(encoding="utf-8") == "header\nline one\nline two\nLINE THREE\n"
    )
    print("PASS combined operations apply")


def test_null_payload_raises_clean_value_error():
    _expect_value_error([{"replace": None}], "must be an object")
    print("PASS null payload raises clean ValueError (was TypeError)")


def test_non_dict_operation_raises_clean_value_error():
    _expect_value_error([["replace", {}]], "exactly one key")
    print("PASS non-dict operation raises clean ValueError")


def test_multi_key_operation_raises_clean_value_error():
    _expect_value_error([{"replace": {}, "delete": {}}], "exactly one key")
    print("PASS multi-key operation raises clean ValueError")


def test_missing_required_key_raises_clean_value_error():
    _expect_value_error(
        [{"replace": {"line_num_or_range": [1, 1]}}], "missing required key"
    )
    _expect_value_error([{"insert_above": {"line_num": 1}}], "missing required key")
    print("PASS missing payload keys raise clean ValueError")


def test_unknown_operation_type_raises_clean_value_error():
    _expect_value_error([{"frobnicate": {}}], "Unknown edit operation type")
    print("PASS unknown operation type raises clean ValueError")


def test_crlf_newline_style_is_preserved():
    path = _make_file()
    path.write_bytes(b"line one\r\nline two\r\nline three\r\n")
    edit_file(
        path,
        [
            {
                "replace": {
                    "line_num_or_range": [2, 2],
                    "replace_with_lines": ["LINE TWO"],
                }
            }
        ],
    )
    assert path.read_bytes() == b"line one\r\nLINE TWO\r\nline three\r\n", (
        "a CRLF file must stay CRLF; forcing LF rewrites files the task"
        " never meant to touch"
    )
    print("PASS CRLF newline style preserved")


def test_missing_trailing_newline_is_preserved():
    path = _make_file()
    path.write_text("line one\nline two\nline three", encoding="utf-8")
    edit_file(
        path,
        [
            {
                "replace": {
                    "line_num_or_range": [1, 1],
                    "replace_with_lines": ["LINE ONE"],
                }
            }
        ],
    )
    assert path.read_text(encoding="utf-8") == "LINE ONE\nline two\nline three", (
        "a file without a trailing newline must not grow one"
    )
    print("PASS missing trailing newline preserved")


def test_non_utf8_bytes_survive_an_unrelated_edit():
    path = _make_file()
    path.write_bytes(b"caf\xe9 menu\nline two\nline three\n")
    edit_file(
        path,
        [
            {
                "replace": {
                    "line_num_or_range": [3, 3],
                    "replace_with_lines": ["LINE THREE"],
                }
            }
        ],
    )
    assert path.read_bytes() == b"caf\xe9 menu\nline two\nLINE THREE\n", (
        "bytes that are not valid UTF-8 must round-trip untouched on an"
        " unrelated edit; errors='replace' corrupted them to U+FFFD"
    )
    print("PASS non-UTF-8 bytes survive unrelated edits")


def test_line_numbers_match_grep_numbering():
    from syna.capabilities.editor import read_file

    path = _make_file()
    path.write_bytes(b"one\ntwo\n\x0cthree has a form feed\nfour\n")
    numbered = read_file(path)
    assert "3  \x0cthree has a form feed" in numbered, (
        "form feeds must not start a new line; splitlines() numbered them"
        " differently from grep -n, sending edits to the wrong line"
    )
    assert "4  four" in numbered
    print("PASS line numbering matches grep -n on form feeds")


def test_replace_string_swaps_the_unique_match():
    path = _make_file()
    edit_file(
        path,
        [{"replace_string": {"old_string": "line two", "new_string": "LINE TWO"}}],
    )
    assert path.read_text(encoding="utf-8") == "line one\nLINE TWO\nline three\n"
    print("PASS replace_string swaps the unique match")


def test_replace_string_requires_exactly_one_match():
    _expect_value_error(
        [{"replace_string": {"old_string": "line", "new_string": "text"}}],
        "found 3 matches",
    )
    _expect_value_error(
        [{"replace_string": {"old_string": "absent text", "new_string": "x"}}],
        "found 0 matches",
    )
    path = _make_file()
    assert path.read_text(encoding="utf-8") == SAMPLE, (
        "a failed replace_string must leave the file untouched"
    )
    print("PASS replace_string enforces the exactly-one-match rule")


def test_replace_string_sees_the_result_of_line_operations():
    path = _make_file()
    edit_file(
        path,
        [
            {"insert_above": {"line_num": 4, "insert_lines": ["unique tail line"]}},
            {
                "replace_string": {
                    "old_string": "unique tail line",
                    "new_string": "unique tail line, edited",
                }
            },
        ],
    )
    assert path.read_text(encoding="utf-8") == (
        "line one\nline two\nline three\nunique tail line, edited\n"
    ), "a replace_string must apply after the batch's line operations"
    print("PASS replace_string composes with line operations")


def main():
    test_valid_edit_still_applies()
    test_valid_combined_operations_still_apply()
    test_null_payload_raises_clean_value_error()
    test_non_dict_operation_raises_clean_value_error()
    test_multi_key_operation_raises_clean_value_error()
    test_missing_required_key_raises_clean_value_error()
    test_unknown_operation_type_raises_clean_value_error()
    test_crlf_newline_style_is_preserved()
    test_missing_trailing_newline_is_preserved()
    test_non_utf8_bytes_survive_an_unrelated_edit()
    test_line_numbers_match_grep_numbering()
    test_replace_string_swaps_the_unique_match()
    test_replace_string_requires_exactly_one_match()
    test_replace_string_sees_the_result_of_line_operations()
    print("ALL EDITOR TESTS PASSED")


if __name__ == "__main__":
    main()
