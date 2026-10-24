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


def main():
    test_valid_edit_still_applies()
    test_valid_combined_operations_still_apply()
    test_null_payload_raises_clean_value_error()
    test_non_dict_operation_raises_clean_value_error()
    test_multi_key_operation_raises_clean_value_error()
    test_missing_required_key_raises_clean_value_error()
    test_unknown_operation_type_raises_clean_value_error()
    print("ALL EDITOR TESTS PASSED")


if __name__ == "__main__":
    main()
