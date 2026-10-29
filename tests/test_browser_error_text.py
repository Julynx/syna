"""Tests for the browser's actionable Playwright error reporting.

Playwright click/type timeouts carry a call log with the actionability
verdict ("element is not visible", "<label> intercepts pointer events",
"element is detached"). The browser tool and the screenshot path condense
errors to one line; that line must keep the reason that lets the model
change course instead of repeating the call.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.capabilities.browser import describe_pw_error


CLICK_TIMEOUT = (
    "ElementHandle.click: Timeout 7000ms exceeded.\n"
    "Call log:\n"
    "  - attempting click action\n"
    "    2 × waiting for element to be visible, enabled and stable\n"
    "      - element is visible, enabled and stable\n"
    "      - scrolling into view if needed\n"
    "      - done scrolling\n"
    '  - <span class="custom-control-input"> intercepts pointer events\n'
    "  - retrying click action\n"
    "    - waiting 20ms\n"
    '  - <span class="custom-control-input"> intercepts pointer events\n'
)


def test_call_log_reason_survives_the_one_line_format():
    text = describe_pw_error(RuntimeError(CLICK_TIMEOUT))
    assert text.startswith("ElementHandle.click: Timeout 7000ms exceeded.")
    assert "intercepts pointer events" in text
    assert "waiting for element" not in text
    assert "retrying" not in text
    assert "\n" not in text
    print("PASS the reason survives even when retry boilerplate precedes it")


def test_reason_lines_are_deduplicated():
    text = describe_pw_error(RuntimeError(CLICK_TIMEOUT))
    assert text.count("intercepts pointer events") == 1
    print("PASS repeated verdict lines collapse to one")


def test_element_state_reasons_are_kept():
    exc_text = (
        "ElementHandle.click: Timeout 7000ms exceeded.\n"
        "Call log:\n"
        "  - waiting for element to be visible, enabled and stable\n"
        "  - element is not visible\n"
    )
    text = describe_pw_error(RuntimeError(exc_text))
    assert "element is not visible" in text
    assert "waiting for element" not in text
    print("PASS state verdicts like 'not visible' survive the noise filter")


def test_long_lines_are_clipped_and_output_bounded():
    exc_text = (
        "click: Timeout exceeded.\n"
        + "x" * 500
        + "\n"
        + "\n".join(f"log line {i}" for i in range(20))
    )
    text = describe_pw_error(RuntimeError(exc_text))
    assert len(text) <= 600
    assert "x" * 500 not in text
    assert "log line 3" in text and "log line 4" not in text
    print("PASS long DOM dumps are clipped and only five log lines are kept")


def test_separators_and_call_log_header_are_dropped():
    text = describe_pw_error(RuntimeError("line one\n========\nCall log:\nline two"))
    assert text == "line one; line two"
    print("PASS separator and header lines are dropped")


def test_empty_error_degrades_gracefully():
    assert describe_pw_error(RuntimeError("")) == "Playwright error"
    assert describe_pw_error(RuntimeError("\n  \n")) == "Playwright error"
    print("PASS an empty error collapses to a placeholder")


def main():
    run_suite(
        [
            test_call_log_reason_survives_the_one_line_format,
            test_long_lines_are_clipped_and_output_bounded,
            test_separators_and_call_log_header_are_dropped,
            test_empty_error_degrades_gracefully,
        ]
    )


if __name__ == "__main__":
    main()
