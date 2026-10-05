import shlex
import shutil
import tempfile
from pathlib import Path
from posixpath import basename, dirname

from .config import truncate_command_output
from .editor import edit_file as e_edit_file
from .editor import read_file as e_read_file
from .state import get_container
from .web_search import search


def web_search(query_or_url: str):
    """
    Search the web or read a web page. Acting on web pages is not supported for now.

    - query_or_url: If of the form "https:/...", will try to load the website.
                    If not, will search it and return the top 10 results, including
                    title, description and url.

    Example:
      {"web_search": {"query_or_url": "Japan"}}
    """
    return search(query_or_url)


def read_file(file_path: str, start: int = 1, limit: int = 200):
    """
    View the contents of a file with line numbers.
    Use this tool over 'execute_command' for file reading.

    - 'file_path' must be an absolute path.
    - 'start' is the first line to be read (lines start at 1).
    - 'limit' is the number of lines to read from 'start'.
        Always set a limit to save tokens.
    - The displayed line numbers are the real file line numbers.
        Use them for 'edit_file'.

    Example:
      {"read_file": {"file_path": "/var/log/app.log", "start": 10, "limit": 30}}
    """
    with tempfile.TemporaryDirectory(prefix="syna-") as temp_dir:
        temp_path = Path(temp_dir)
        container = get_container()
        container.copy_from(file_path, str(temp_path))
        output = e_read_file(Path(temp_path, basename(file_path)), start, limit)
    return output


def edit_file(file_path: str, operations: list[dict]):
    """
    Edit the contents of a file.
    Use this tool over 'execute_command' for file editing.

    - 'file_path' must be an absolute path.
    - 'operations' is a list of dicts, each one of:
        {"insert_above": {"line_num": 3, "insert_lines": ["..."]}}
        {"replace": {"line_num_or_range": [45, 47], "replace_with_lines": ["..."]}}
        {"delete": {"line_num_or_range": [45, 47]}}   # ranges are inclusive
    - Line numbers are 1-based and always refer to the ORIGINAL file, as shown
      by 'read_file'. Combine several operations in one call.

    Example:
      {"edit_file": {"file_path": "/tmp/notes.txt", "operations": [
        {"replace": {"line_num_or_range": [2, 2], "replace_with_lines": ["new line"]}},
        {"insert_above": {"line_num": 4, "insert_lines": ["inserted line"]}},
        {"delete": {"line_num_or_range": [6, 7]}}
      ]}}
    """
    with tempfile.TemporaryDirectory(prefix="syna-") as temp_dir:
        temp_path = Path(temp_dir)
        container = get_container()
        container.copy_from(file_path, str(temp_path))
        temp_file_path = Path(temp_path, basename(file_path))
        e_edit_file(temp_file_path, operations)
        container.copy_to(str(temp_file_path), dirname(file_path) or "/")
    return f"File edited successfully ({len(operations)} operations applied)."


def execute_command(command: str, arguments: list[str] | None = None):
    """
    Execute a command inside the container's debian machine and get its output.
    You have full control over this machine: you may install packages, create
    and run scripts, and take any action you need to perform your task.

    Two accepted calling styles:
    - Shell style: 'command' is a full shell line, exactly as you would type it
      in a bash terminal. Leave 'arguments' empty. Pipes, redirection, && and
      quotes all work.
    - Argv style: 'command' is the bare executable name and 'arguments' is the
      list of words after it.

    Examples:
      {"execute_command": {"command": "grep -rn 'def main' src/ | head -5"}}
      {"execute_command": {"command": "ls", "arguments": ["-la", "/tmp"]}}
    """
    if arguments:
        argv = [command, *arguments]
    else:
        argv = ["bash", "-c", command]
    container = get_container()
    response = container.exec(argv)
    return truncate_command_output(response)


def expose_file(file_path: str):
    """
    Share a file or directory with the user through the chat interface.
    ALWAYS call this tool for every file you want the user to see or keep,
    such as reports, images, documents or any generated output files.
    Files that live inside the pod are NOT reachable by the user in any
    other way: if you do not expose a file with this tool, the user will
    not be able to access it.

    - 'file_path' must be an absolute path to an existing file or directory.
    - Directories are exposed as a single downloadable zip archive.

    Example:
      {"expose_file": {"file_path": "/home/user/report.pdf"}}
    """
    container = get_container()
    quoted_path = shlex.quote(file_path)
    probe = container.exec(
        ["bash", "-c",
         f"if [ -f {quoted_path} ]; then echo file;"
         f" elif [ -d {quoted_path} ]; then echo dir;"
         f" else echo missing; fi"]
    ).strip()
    if probe == "missing":
        return {"error": f"Path not found inside the pod: {file_path}"}
    return {
        "path": file_path,
        "name": basename(file_path.rstrip("/")) or file_path,
        "is_dir": probe == "dir",
    }


def respond(text: str):
    """
    Send a message to the user. This will end the task and pass priority to him.
    DO NOT include a call to this tool in the same message as other tool calls.
    If you are still working on a task, only call this tool when you have finished.
    """
    return text
