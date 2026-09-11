import ast


def get_function_metadata(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=file_path)

    functions_info = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Extract arguments and their type annotations
            args = []
            for arg in node.args.args:
                arg_info = {
                    "name": arg.arg,
                    "type": ast.unparse(arg.annotation) if arg.annotation else None,
                }
                args.append(arg_info)

            # Handle *args and **kwargs
            if node.args.vararg:
                args.append(
                    {
                        "name": f"*{node.args.vararg.arg}",
                        "type": ast.unparse(node.args.vararg.annotation)
                        if node.args.vararg.annotation
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
                    }
                )

            # Extract return type annotation
            return_type = ast.unparse(node.returns) if node.returns else None

            # Extract docstring
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
