from pathlib import Path


"""Line-based file reading and editing helpers shared by the agent tools."""


def with_line_numbers(text, start=1, limit=200):
    """Render text with 1-based line numbers, reading `limit` lines from `start`."""
    start -= 1
    lines = text.splitlines()
    lines_dict = dict(enumerate(lines[start:start + limit], start=start + 1))

    max_lines_digits = len(str(abs(len(lines))))

    return_lines = []
    for key, value in lines_dict.items():
        key_str: str = str(key)
        return_lines.append(f"{key_str.ljust(max_lines_digits)}  {value}")

    return "\n".join(return_lines)


def read_file(file: Path | str, start=1, limit=200):
    text = Path(file).read_text()
    return with_line_numbers(text, start, limit)


def edit_file(file_path, operations: list[dict]):
    """
    operations is a list of dicts, each one of:
        {"insert_above": {"line_num": 3, "insert_lines": ["..."]}}
        {"replace": {"line_num_or_range": [45, 47], "replace_with_lines": ["..."]}}
        {"delete": {"line_num_or_range": [45, 47]}}   # ranges are inclusive
    Line numbers are 1-based and refer to the ORIGINAL file.
    Raises ValueError, before touching the file, for unknown operations,
    out-of-bounds line numbers, invalid ranges or overlapping edit ranges.
    """
    inserts: dict[int, list[str]] = {}
    removed: set[int] = set()
    replacements: dict[int, list[str]] = {}

    lines = Path(file_path).read_text().splitlines()
    occupied: set[int] = set()

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

    for operation in operations:
        for op_type, data in operation.items():
            if op_type == "insert_above":
                line_num = data["line_num"]
                if not isinstance(line_num, int) or not 1 <= line_num <= len(lines) + 1:
                    raise ValueError(
                        f"insert_above line_num {line_num!r} is out of bounds"
                        f" for a file with {len(lines)} lines"
                        f" (valid: 1..{len(lines) + 1})"
                    )
                inserts.setdefault(line_num, []).extend(data["insert_lines"])
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
                        data["replace_with_lines"]
                    )
            else:
                raise ValueError(f"Unknown edit operation type: {op_type!r}")

    result: list[str] = []

    for idx, line in enumerate(lines, start=1):
        result.extend(inserts.get(idx, []))
        result.extend(replacements.get(idx, []))
        if idx not in removed:
            result.append(line)

    result.extend(inserts.get(len(lines) + 1, []))
    result = "\n".join(result) + "\n"
    Path(file_path).write_text(result, newline="\n")
