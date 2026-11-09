"""Validation of the tool schemas the model actually receives.

Tool docstrings in tools.py are the model-facing descriptions: the
schema generator splits them into a description plus per-argument
documentation, and a parsing error there (an argument bullet the
pattern misses, a typo in a documented name, prose swallowed as
argument documentation, a required-annotation contradiction) silently
degrades what the model sees. These tests pin the whole generated set
against the real signatures in TOOL_REGISTRY.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.capabilities.tools import TOOL_REGISTRY
from syna.protocol.tool_schemas import (
    _python_type_to_json_schema,
    _split_docstring,
    get_tool_definitions,
)

JSON_SCHEMA_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


def _schemas_by_name():
    return {
        schema["function"]["name"]: schema["function"]
        for schema in get_tool_definitions()
    }


def _signature_arguments(tool):
    return [
        parameter
        for parameter in inspect.signature(tool).parameters.values()
        if parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
    ]


def test_every_registered_tool_has_exactly_one_schema():
    schemas = _schemas_by_name()
    missing = sorted(set(TOOL_REGISTRY) - set(schemas))
    assert not missing, f"registered tools without a schema: {missing}"
    extra = sorted(set(schemas) - set(TOOL_REGISTRY))
    assert not extra, f"schemas without a registered tool: {extra}"


def test_every_tool_and_argument_is_documented():
    """Every tool has a description; every argument has its docstring line.

    A missing argument description means the generator dropped the
    bullet (pattern mismatch) or the docstring never documented the
    argument; both silently blind the model about that argument.
    """
    schemas = _schemas_by_name()
    for name, function in sorted(schemas.items()):
        assert function["description"].strip(), f"{name} has no description"
        properties = function["parameters"]["properties"]
        assert properties, f"{name} declares no arguments"
        for argument, prop_schema in sorted(properties.items()):
            assert prop_schema.get("description", "").strip(), (
                f"{name}.{argument} has no description"
            )


def test_documented_arguments_match_the_signature_exactly():
    """No phantom docs, no undocumented arguments, no swallowed prose.

    _split_docstring lifts `- name: text` lines out of the description
    into argument documentation. A bullet whose name is not a real
    signature argument is a parsing error: the text vanishes from the
    description and documents nothing. This catches typos and prose
    bullets shaped like `- word: ...` alike.
    """
    schemas = _schemas_by_name()
    for name, function in sorted(schemas.items()):
        actual = {p.name for p in _signature_arguments(TOOL_REGISTRY[name])}
        documented = set(function["parameters"]["properties"])
        assert documented == actual, (
            f"{name}: documented {sorted(documented)} vs signature {sorted(actual)}"
        )


def test_required_lists_match_the_signatures():
    """Schema required == arguments without defaults (none are Optional)."""
    schemas = _schemas_by_name()
    for name, function in sorted(schemas.items()):
        signature_arguments = _signature_arguments(TOOL_REGISTRY[name])
        expected = [p.name for p in signature_arguments if p.default is p.empty]
        assert function["parameters"]["required"] == expected, name
        for p in signature_arguments:
            if p.default is p.empty:
                annotation = str(p.annotation) if p.annotation is not p.empty else ""
                assert "None" not in annotation, (
                    f"{name}.{p.name}: Optional-typed without a default is"
                    " excluded from required but cannot be omitted"
                )


def test_property_types_are_valid_json_schema():
    schemas = _schemas_by_name()
    for name, function in sorted(schemas.items()):
        for argument, prop_schema in function["parameters"]["properties"].items():
            assert prop_schema.get("type") in JSON_SCHEMA_TYPES, (
                f"{name}.{argument}: {prop_schema}"
            )
            if prop_schema["type"] == "array":
                assert "items" in prop_schema, f"{name}.{argument}: untyped items"


def test_arg_bullets_never_leak_into_the_description():
    """The splitter must remove every argument bullet from the body."""
    schemas = _schemas_by_name()
    for name, function in sorted(schemas.items()):
        for line in function["description"].splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                assert "(" not in stripped.split(":")[0], (
                    f"{name}: an argument bullet leaked into the description:"
                    f" {stripped}"
                )


def test_type_mapping_covers_the_annotation_grammar():
    assert _python_type_to_json_schema("str") == {"type": "string"}
    assert _python_type_to_json_schema("int") == {"type": "integer"}
    assert _python_type_to_json_schema("dict | None") == {"type": "object"}
    assert _python_type_to_json_schema("list[str]") == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert _python_type_to_json_schema("bool | None") == {"type": "boolean"}


def test_split_docstring_keeps_prose_bullets_in_the_body():
    """Only argument-shaped bullets become argument documentation."""
    description, arguments = _split_docstring(
        "Summary line.\n\n"
        "- `name` (string, required): the name.\n"
        "- A prose bullet about behavior.\n"
        "- `ghost_name`: a typo'd argument."
    )
    assert "Summary line." in description
    assert "A prose bullet about behavior." in description
    assert arguments == {"name": "the name.", "ghost_name": "a typo'd argument."}


def test_dispatch_schemas_declare_both_arguments_required():
    """The dispatch tools' schema matches their documented contract.

    Both docstrings say `args` is required; the signature must agree,
    or the schema tells the model the arguments object is optional and
    invites top-level inlining (the observed first-call failure).
    """
    schemas = _schemas_by_name()
    for name in ("process", "subagent"):
        parameters = schemas[name]["parameters"]
        assert parameters["required"] == ["action", "args"], (name, parameters)
        action_doc = parameters["properties"]["action"]["description"]
        assert "spawn" in action_doc and "check" in action_doc, name


def main():
    run_suite(
        [
            test_every_registered_tool_has_exactly_one_schema,
            test_every_tool_and_argument_is_documented,
            test_documented_arguments_match_the_signature_exactly,
            test_required_lists_match_the_signatures,
            test_property_types_are_valid_json_schema,
            test_arg_bullets_never_leak_into_the_description,
            test_type_mapping_covers_the_annotation_grammar,
            test_split_docstring_keeps_prose_bullets_in_the_body,
            test_dispatch_schemas_declare_both_arguments_required,
        ]
    )


if __name__ == "__main__":
    main()
