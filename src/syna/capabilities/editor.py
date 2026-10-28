"""Line-based file reading and editing helpers shared by the agent tools.

Files round-trip byte-exactly: reads decode with `surrogateescape`, so
bytes that are not valid UTF-8 survive an unrelated edit untouched (the
numbered display maps them to U+FFFD, matching the old behavior), and
writes re-encode the same way. Content is split on `\n` only — matching
`grep -n` and shell line tools — and a file's newline style and missing
trailing newline are preserved on write instead of being normalized.
"""

from pathlib import Path

_READ_ENCODING = "utf-8"
_ROUNDTRIP_ERRORS = "surrogateescape"


def _read_lines(file) -> tuple[list[str], bool, bool]:
    """Read a file as editable lines; return (lines, uses_crlf, ends_on_newline).

    The split is on `\n` alone; each line's trailing `\r` is stripped and
    remembered as the file's CRLF flag, so line numbers and content match
    what `grep -n` reports. A final empty line is not produced for files
    that end on a newline.
    """
    raw = Path(file).read_bytes().decode(_READ_ENCODING, errors=_ROUNDTRIP_ERRORS)
    if raw == "":
        return [], False, False
    ends_on_newline = raw.endswith("\n")
    body = raw[:-1] if ends_on_newline else raw
    uses_crlf = "\r\n" in raw
    lines = [line[:-1] if line.endswith("\r") else line for line in body.split("\n")]
    return lines, uses_crlf, ends_on_newline


def _write_content(file, content: str, uses_crlf: bool, ends_on_newline: bool):
    """Re-apply the file's newline style and write the bytes back."""
    if ends_on_newline:
        content += "\n"
    if uses_crlf:
        content = content.replace("\n", "\r\n")
    Path(file).write_bytes(content.encode(_READ_ENCODING, errors=_ROUNDTRIP_ERRORS))


def _display_safe(text: str) -> str:
    """Map undecodable-byte surrogates to U+FFFD for display only."""
    return text.encode("utf-8", errors="surrogatepass").decode(
        "utf-8", errors="replace"
    )


def with_line_numbers(lines, start=1, limit=200):
    """Render editable lines with 1-based numbers, `limit` lines from `start`."""
    start -= 1
    window = list(enumerate(lines[start : start + limit], start=start + 1))

    max_lines_digits = len(str(max(1, len(lines))))

    return_lines = []
    for key, value in window:
        key_str: str = str(key)
        return_lines.append(f"{key_str.ljust(max_lines_digits)}  {value}")

    return "\n".join(return_lines)


def read_file(file: Path | str, start=1, limit=200):
    lines, _, _ = _read_lines(file)
    return with_line_numbers([_display_safe(line) for line in lines], start, limit)


def edit_file(file_path, operations: list[dict]):
    """
    operations is a list of dicts, each one of:
        {"insert_above": {"line_num": 3, "insert_lines": ["..."]}}
        {"replace": {"line_num_or_range": [45, 47], "replace_with_lines": ["..."]}}
        {"delete": {"line_num_or_range": [45, 47]}}   # ranges are inclusive
        {"replace_string": {"old_string": "...", "new_string": "..."}}
    Line numbers are 1-based, refer to the ORIGINAL file, and count lines
    the same way `grep -n` does. `replace_string` swaps the one exact
    occurrence of old_string for new_string and fails unless old_string
    matches exactly once; it runs after the line operations and is the
    safer choice for small edits in files that may have changed.
    Provided lines and strings are normalized to the file's newline
    style. The file's newline style and trailing-newline presence are
    preserved, and bytes that are not valid UTF-8 round-trip untouched.
    Raises ValueError, before touching the file, for malformed operations,
    unknown operation types, out-of-bounds line numbers, invalid ranges,
    overlapping edit ranges, or a replace_string that matches zero or
    several times.
    """
    inserts: dict[int, list[str]] = {}
    removed: set[int] = set()
    replacements: dict[int, list[str]] = {}
    string_replacements: list[tuple[str, str]] = []

    lines, uses_crlf, ends_on_newline = _read_lines(file_path)
    occupied: set[int] = set()

    required_keys = {
        "insert_above": ("line_num", "insert_lines"),
        "replace": ("line_num_or_range", "replace_with_lines"),
        "delete": ("line_num_or_range",),
        "replace_string": ("old_string", "new_string"),
    }
    operation_types = ", ".join(sorted(required_keys))

    def _resolve_line_num_or_range(data) -> tuple[int, int]:
        ref = data["line_num_or_range"]
        start, end = ref if isinstance(ref, (list, tuple)) else (ref, ref)
        if not (isinstance(start, int) and isinstance(end, int)):
            raise ValueError(f"line_num_or_range must be integers, got: {ref!r}")
        if start > end:
            raise ValueError(f"Invalid range: start {start} is after end {end}")
        if not 1 <= start <= len(lines) or not 1 <= end <= len(lines):
            raise ValueError(
                f"Line range [{start}, {end}] is out of bounds"
                f" for a file with {len(lines)} lines"
            )
        return start, end

    def _parse_operation(operation) -> tuple[str, dict]:
        if not isinstance(operation, dict) or len(operation) != 1:
            raise ValueError(
                "Each edit operation must be an object with exactly one key"
                f" ({operation_types}); got {operation!r}"
            )
        op_type, data = next(iter(operation.items()))
        if op_type not in required_keys:
            raise ValueError(f"Unknown edit operation type: {op_type!r}")
        if not isinstance(data, dict):
            raise ValueError(
                f"The payload for '{op_type}' must be an object; got {data!r}"
            )
        missing = [key for key in required_keys[op_type] if key not in data]
        if missing:
            raise ValueError(
                f"The payload for '{op_type}' is missing required"
                f" key(s): {', '.join(missing)}"
            )
        return op_type, data

    for operation in operations:
        op_type, data = _parse_operation(operation)
        if op_type == "insert_above":
            line_num = data["line_num"]
            if not isinstance(line_num, int) or not 1 <= line_num <= len(lines) + 1:
                raise ValueError(
                    f"insert_above line_num {line_num!r} is out of bounds"
                    f" for a file with {len(lines)} lines"
                    f" (valid: 1..{len(lines) + 1})"
                )
            inserts.setdefault(line_num, []).extend(
                line.replace("\r", "") for line in data["insert_lines"]
            )
        elif op_type in ("replace", "delete"):
            start, end = _resolve_line_num_or_range(data)
            overlap = occupied.intersection(range(start, end + 1))
            if overlap:
                raise ValueError(
                    f"{op_type} range [{start}, {end}] overlaps"
                    f" previously edited line(s): {sorted(overlap)}"
                )
            occupied.update(range(start, end + 1))
            removed.update(range(start, end + 1))
            if op_type == "replace":
                replacements.setdefault(start, []).extend(
                    line.replace("\r", "") for line in data["replace_with_lines"]
                )
        else:
            old_string, new_string = data["old_string"], data["new_string"]
            if not isinstance(old_string, str) or not old_string:
                raise ValueError(
                    "replace_string old_string must be a non-empty string;"
                    f" got {old_string!r}"
                )
            if not isinstance(new_string, str):
                raise ValueError(
                    f"replace_string new_string must be a string; got {new_string!r}"
                )
            string_replacements.append(
                (old_string.replace("\r", ""), new_string.replace("\r", ""))
            )

    result: list[str] = []

    for idx, line in enumerate(lines, start=1):
        result.extend(inserts.get(idx, []))
        result.extend(replacements.get(idx, []))
        if idx not in removed:
            result.append(line)

    result.extend(inserts.get(len(lines) + 1, []))
    content = "\n".join(result)

    for old_string, new_string in string_replacements:
        matches = content.count(old_string)
        if matches == 0:
            raise ValueError(
                "replace_string found 0 matches; copy old_string exactly"
                " from the read_file output, including whitespace and"
                " indentation."
            )
        if matches > 1:
            raise ValueError(
                f"replace_string found {matches} matches; include more"
                " surrounding text in old_string so exactly one matches."
            )
        content = content.replace(old_string, new_string)

    _write_content(file_path, content, uses_crlf, ends_on_newline)
