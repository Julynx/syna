"""Extract function names, docstrings and argument types via ast."""

import ast


def get_function_metadata(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        tree = ast.parse(file.read(), filename=file_path)

    functions_info = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue

            args = []
            positional = node.args.args
            defaults = node.args.defaults
            first_default = len(positional) - len(defaults)
            for index, arg in enumerate(positional):
                has_default = index >= first_default
                args.append(
                    {
                        "name": arg.arg,
                        "type": ast.unparse(arg.annotation) if arg.annotation else None,
                        "has_default": has_default,
                        "default": ast.unparse(defaults[index - first_default])
                        if has_default
                        else None,
                    }
                )

            if node.args.vararg:
                args.append(
                    {
                        "name": f"*{node.args.vararg.arg}",
                        "type": ast.unparse(node.args.vararg.annotation)
                        if node.args.vararg.annotation
                        else None,
                        "has_default": False,
                        "default": None,
                    }
                )
            for arg, default in zip(
                node.args.kwonlyargs, node.args.kw_defaults, strict=False
            ):
                args.append(
                    {
                        "name": arg.arg,
                        "type": ast.unparse(arg.annotation) if arg.annotation else None,
                        "has_default": default is not None,
                        "default": ast.unparse(default)
                        if default is not None
                        else None,
                    }
                )
            if node.args.kwarg:
                args.append(
                    {
                        "name": f"**{node.args.kwarg.arg}",
                        "type": ast.unparse(node.args.kwarg.annotation)
                        if node.args.kwarg.annotation
                        else None,
                        "has_default": False,
                        "default": None,
                    }
                )

            return_type = ast.unparse(node.returns) if node.returns else None

            docstring = ast.get_docstring(node)

            functions_info.append(
                {
                    "name": node.name,
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "line_number": node.lineno,
                    "arguments": args,
                    "return_type": return_type,
                    "docstring": docstring,
                }
            )

    return functions_info
