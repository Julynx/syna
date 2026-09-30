from pathlib import Path


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
    """
    inserts: dict[int, list[str]] = {}
    removed: set[int] = set()
    replacements: dict[int, list[str]] = {}

    for operation in operations:
        for op_type, data in operation.items():
            if op_type == "insert_above":
                inserts.setdefault(data["line_num"], []).extend(data["insert_lines"])
            elif op_type in ("replace", "delete"):
                ref = data["line_num_or_range"]
                start, end = ref if isinstance(ref, (list, tuple)) else (ref, ref)
                removed.update(range(start, end + 1))
                if op_type == "replace":
                    replacements.setdefault(start, []).extend(
                        data["replace_with_lines"]
                    )

    lines = Path(file_path).read_text().splitlines()
    result: list[str] = []

    for idx, line in enumerate(lines, start=1):
        result.extend(inserts.get(idx, []))
        result.extend(replacements.get(idx, []))
        if idx not in removed:
            result.append(line)

    result.extend(inserts.get(len(lines) + 1, []))
    result = "\n".join(result) + "\n"
    Path(file_path).write_text(result, newline="\n")


# result = read_file("test.txt")
# print(result)
# print("---")
# edit_file(
#     "test.txt",
#     [
#         {"delete": {"line_num_or_range": [9, 13]}},
#     ],
# )
# result = read_file("test.txt")
# print(result)
# print("---")
