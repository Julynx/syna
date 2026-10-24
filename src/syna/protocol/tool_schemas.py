"""JSON Schema generation for tools registered in Syna.

Generates OpenAPI / OpenAI-compatible tool specifications for OpenRouter from
Python function metadata and docstrings in tools.py.
"""

import re
from typing import Any

from .inspect_module import get_function_metadata

ARG_DOCSTRING_PATTERN = re.compile(
    r"^\s*-\s*[`'\"]?(\w+)[`'\"]?\s*(?:\([^)]*\))?\s*(?::\s*(.*))?$",
    re.MULTILINE,
)


def _python_type_to_json_schema(type_str: str | None) -> dict[str, Any]:
    """Map a Python type annotation string to a JSON Schema snippet."""
    if not type_str:
        return {"type": "string"}

    raw = type_str.strip()
    parts = [part.strip() for part in raw.split("|")]
    non_none_parts = [part for part in parts if part != "None"]
    effective = non_none_parts[0] if non_none_parts else "string"

    if effective == "str":
        return {"type": "string"}
    if effective == "int":
        return {"type": "integer"}
    if effective == "float":
        return {"type": "number"}
    if effective == "bool":
        return {"type": "boolean"}
    if effective.startswith("list[") and effective.endswith("]"):
        inner = effective[5:-1].strip()
        inner_schema = _python_type_to_json_schema(inner)
        return {"type": "array", "items": inner_schema}
    if effective == "list":
        return {"type": "array"}
    if effective == "dict" or effective.startswith("dict["):
        return {"type": "object"}
    return {"type": "string"}


def _split_docstring(docstring: str | None) -> tuple[str, dict[str, str]]:
    """Split a docstring into a tool description and per-argument descriptions."""
    if not docstring:
        return "", {}

    arg_descriptions: dict[str, str] = {}
    lines = docstring.strip().splitlines()
    body_lines: list[str] = []

    for line in lines:
        match = ARG_DOCSTRING_PATTERN.match(line)
        if match:
            arg_name = match.group(1)
            desc = match.group(2) or ""
            arg_descriptions[arg_name] = desc.strip()
        else:
            body_lines.append(line)

    cleaned_description = "\n".join(body_lines).strip()
    return cleaned_description, arg_descriptions


def function_metadata_to_tool_schema(metadata: dict[str, Any]) -> dict[str, Any]:
    """Convert one function metadata entry to an OpenRouter function tool spec."""
    name = metadata["name"]
    docstring = metadata.get("docstring") or ""
    tool_description, arg_descriptions = _split_docstring(docstring)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for arg in metadata.get("arguments", []):
        arg_name = arg["name"]
        if arg_name.startswith("*"):
            continue

        prop_schema = _python_type_to_json_schema(arg.get("type"))
        desc = arg_descriptions.get(arg_name)
        if desc:
            prop_schema["description"] = desc

        properties[arg_name] = prop_schema

        type_str = arg.get("type") or ""
        is_optional = "None" in [p.strip() for p in type_str.split("|")]
        if not arg.get("has_default") and not is_optional:
            required.append(arg_name)

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": tool_description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def get_tool_definitions(tools_path=None) -> list[dict[str, Any]]:
    """Return OpenRouter-compatible tool schemas for all tools in tools.py."""
    if tools_path is None:
        from ..capabilities import tools

        tools_path = tools.__file__

    metadata_list = get_function_metadata(tools_path)
    from ..capabilities.tools import TOOL_REGISTRY

    allowed_names = set(TOOL_REGISTRY.keys())

    schemas = []
    for metadata in metadata_list:
        if metadata["name"] in allowed_names:
            schemas.append(function_metadata_to_tool_schema(metadata))
    return schemas
