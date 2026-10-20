"""The tools exposed to the model, and the whitelists that dispatch them.

TOOL_REGISTRY and BROWSER_COMMANDS are explicit whitelists: parse.py
dispatches through TOOL_REGISTRY instead of getattr on the module, and
web_interact dispatches through BROWSER_COMMANDS instead of getattr on the
Browser object. That keeps internals (editor primitives, path utilities,
browser internals like run()/page/close()) out of the model's reach.

Every tool docstring is injected verbatim into the system prompt
(`build_prompt`), so the docstrings below are the model-facing tool
documentation and must stay in sync with actual behavior.
"""

import re
import shlex
import tempfile
from pathlib import Path
from posixpath import basename, dirname
from typing import Any

from .browser import Browser
from .config import (
    get_logger,
    load_command_timeout_s,
    load_output_limit_chars,
    load_tool_call_timeout_s,
)
from .editor import edit_file as e_edit_file
from .editor import read_file as e_read_file
from .state import get_call_output_path, get_container, set_last_exit_code
from .vision import describe_image as v_describe_image
from .web_search import search

BROWSER = None

BROWSER_COMMANDS = {
    "goto": Browser.goto,
    "snapshot": Browser.snapshot,
    "click": Browser.click,
    "type": Browser.type,
    "select": Browser.select,
    "press": Browser.press,
    "scroll": Browser.scroll,
    "back": Browser.back,
    "read": Browser.read,
}

NONINTERACTIVE_ENV_EXPORTS = (
    "DEBIAN_FRONTEND=noninteractive PAGER=cat GIT_PAGER=cat"
    " GIT_TERMINAL_PROMPT=0 PIP_NO_INPUT=1 PIP_PROGRESS_BAR=off"
    " PYTHONUNBUFFERED=1 TERM=dumb NO_COLOR=1"
)

EXIT_CODE_MARKER_PATTERN = re.compile(r"\n<<<SYNA_EXIT=(\d+)>>>\s*$")

COMMAND_TIMEOUT_MARGIN_S = 5
COMMAND_KILL_GRACE_S = 5


def think(thought: str):
    """
    Record reasoning in the conversation. It changes nothing in the pod and returns nothing. Your private reasoning may not carry over between turns, but `think` entries stay visible for the rest of the task. Use it when the note will matter later: a plan for multi-step work, what you concluded from a surprising or failed result, a decision and its reason, and a check of your claims against the evidence before `respond`. Skip it for routine steps. Pair `think` with the call or `respond` it informs in the same reply when you can: a reply containing only `think` is valid but costs an extra round-trip, so use it only when the note matters and there is nothing to act on yet. It is not a message to the user; anything they need to know goes in `respond`.
    - `thought` (string, required)
    """
    return "(noted)"


def execute_command(command: str, arguments: list[str] | None = None):
    """
    Run a command in the pod (behavior described under Environment).
    - `command` (string, required): a full bash command line. Pipes, redirection, `&&`, and quoting work.
    - `arguments` (list of strings, optional): if present, `command` must be a bare executable name, and these are its arguments, passed without shell processing. Useful when quoting gets awkward.

    To create a file, use a quoted heredoc so the shell expands nothing: `cat > /workspace/report/build.py <<'EOF'` … `EOF`.

    Example: `{"execute_command": {"command": "cd /workspace/app && python3 -m pytest -q 2>&1 | tail -n 30"}}`
    """
    container = get_container()
    output_path = get_call_output_path()
    if not output_path:
        argv = [command, *arguments] if arguments else ["bash", "-c", command]
        return container.exec(argv)

    timeout_s = _effective_command_timeout()
    exit_path = output_path + ".exit"
    if arguments:
        timed_command = "timeout -k {} {} {}".format(
            COMMAND_KILL_GRACE_S,
            timeout_s,
            " ".join(shlex.quote(part) for part in [command, *arguments]),
        )
    else:
        timed_command = (
            f"timeout -k {COMMAND_KILL_GRACE_S} {timeout_s} bash -c"
            f" {shlex.quote(command)}"
        )

    runner_script = "\n".join(
        [
            "mkdir -p /workspace /tool_outputs",
            "cd /workspace || cd /",
            f"exec > {shlex.quote(output_path)} 2>&1",
            f"export {NONINTERACTIVE_ENV_EXPORTS}",
            timed_command,
            f'printf %s "$?" > {shlex.quote(exit_path)}',
        ]
    )
    container.exec(["bash", "-c", runner_script])
    view = container.exec(["bash", "-c", _reader_script(output_path, exit_path)])

    match = EXIT_CODE_MARKER_PATTERN.search(view)
    if match:
        set_last_exit_code(int(match.group(1)))
        view = view[: match.start()]
    else:
        set_last_exit_code(None)
    return view


def _effective_command_timeout() -> int:
    """Bound the command timeout by the harness's tool-call detach timeout."""
    detach_timeout = load_tool_call_timeout_s()
    return max(
        5, min(load_command_timeout_s(), int(detach_timeout) - COMMAND_TIMEOUT_MARGIN_S)
    )


def _reader_script(output_path: str, exit_path: str) -> str:
    """Build the pod-side script returning a bounded view of the output file."""
    quoted_output = shlex.quote(output_path)
    quoted_exit = shlex.quote(exit_path)
    return (
        f"code=$(cat {quoted_exit} 2>/dev/null || echo 137)\n"
        f"size=$(wc -c < {quoted_output} 2>/dev/null || echo 0)\n"
        f"limit={load_output_limit_chars()}\n"
        'if [ "$size" -eq 0 ]; then\n'
        "  printf '(no output)'\n"
        'elif [ "$size" -le "$limit" ]; then\n'
        f"  cat {quoted_output}\n"
        "else\n"
        f'  head -c $((limit / 2)) {quoted_output}\n'
        "  printf '\\n[... %s bytes omitted; full output in %s ...]\\n'"
        f' "$((size - limit))" {quoted_output}\n'
        f'  tail -c $((limit / 2)) {quoted_output}\n'
        "fi\n"
        f"rm -f {quoted_exit}\n"
        'printf "\\n<<<SYNA_EXIT=%s>>>\\n" "$code"'
    )


def web_search(query: str):
    """
    Search the web. Returns the top 10 results with title, description, and URL. Snippets can be outdated or out of context: open the page before relying on a claim.
    - `query` (string, required)

    Example: `{"web_search": {"query": "debian 13 release notes"}}`
    """
    return search(query)


def web_interact(command: str, args: dict[str, Any]):
    """
    Control a persistent browser that runs JavaScript; the current page, cookies, and logins persist between calls. Use it to read and interact with web pages, since many sites block plain HTTP clients. Use `execute_command` (`curl`, `wget`) to download files and call APIs.

    Arguments: `command` (string, required) and `args` (object).
    - `goto`: `url` (string, required). Opens a page and returns a compact view. Interactive elements appear like `[12] button "Buy"`; pass the number as `ref`.
    - `snapshot`: `viewport_only` (boolean). Re-reads the current page.
    - `click`: `ref` (integer, required). Returns what changed.
    - `type`: `ref` (integer, required), `text` (string, required), `clear` (boolean, default true), `submit` (boolean; presses Enter after typing).
    - `select`: `ref` (integer, required), `option` (string, required): visible label or value.
    - `press`: `key` (string, required), e.g. `Enter`, `Escape`, `Tab`, `ArrowDown`.
    - `scroll`: `direction` (`down`, `up`, `top`, or `bottom`), `amount` (fraction of viewport height, default 0.8). Returns the visible viewport.
    - `back`: no arguments.
    - `read`: `ref` (integer; omit for the whole page), `max_chars` (integer). Full plain text, for when a view truncated something you need.

    Refs belong to the page state they came from; after navigation or large page changes, use refs from the newest output. Submitting forms that send, post, or buy something needs approval (see Protecting the user).

    Example: `{"web_interact": {"command": "type", "args": {"ref": 7, "text": "debian 13 release notes", "submit": true}}}`
    """
    global BROWSER

    if not BROWSER:
        BROWSER = Browser()

    if not args:
        args = {}

    function = BROWSER_COMMANDS.get(command)
    if function is None:
        return (
            f"ERROR: unknown browser command '{command}'. Available commands:"
            f" {', '.join(sorted(BROWSER_COMMANDS))}"
        )
    return function(BROWSER, **args)


def read_file(file_path: str, start: int = 1, limit: int = 200):
    """
    Read a text file with line numbers. Prefer this to `cat`.
    - `file_path` (string, required): absolute path.
    - `start` (integer, required): first line to read, counting from 1.
    - `limit` (integer, required): number of lines. Keep it small; in large files, find the relevant lines first with `grep -n`.

    The numbers shown are the file's real line numbers; use them with `edit_file`.

    Example: `{"read_file": {"file_path": "/workspace/app/main.py", "start": 1, "limit": 80}}`
    """
    with tempfile.TemporaryDirectory(prefix="syna-") as temp_dir:
        temp_path = Path(temp_dir)
        container = get_container()
        container.copy_from(file_path, str(temp_path))
        output = e_read_file(Path(temp_path, basename(file_path)), start, limit)
    return output


def edit_file(file_path: str, operations: list[dict]):
    """
    Edit a text file by line number. Prefer this to `sed` or rewriting the whole file.
    - `file_path` (string, required): absolute path.
    - `operations` (list, required): one or more of
      - `{"insert_above": {"line_num": 3, "insert_lines": ["..."]}}`
      - `{"replace": {"line_num_or_range": [45, 47], "replace_with_lines": ["..."]}}`
      - `{"delete": {"line_num_or_range": [45, 47]}}`

    Ranges are inclusive. Every line number in a call refers to the file as it was before that call, so several operations can be combined without recalculating. Afterwards, numbering has shifted: re-read the affected lines before the next edit, which also confirms the edit landed as intended. Write each line with its full indentation.

    Example: `{"edit_file": {"file_path": "/workspace/app/config.py", "operations": [{"replace": {"line_num_or_range": [12, 13], "replace_with_lines": ["TIMEOUT_SECONDS = 30", "MAX_RETRIES = 3"]}}]}}`
    """
    with tempfile.TemporaryDirectory(prefix="syna-") as temp_dir:
        temp_path = Path(temp_dir)
        container = get_container()
        container.copy_from(file_path, str(temp_path))
        temp_file_path = Path(temp_path, basename(file_path))
        e_edit_file(temp_file_path, operations)
        container.copy_to(str(temp_file_path), dirname(file_path) or "/")
    return f"File edited successfully ({len(operations)} operations applied)."


def describe_image(file_path: str, question: str):
    """
    Get a text description of an image file, focused on your question. Useful for screenshots, charts, and photos, and for checking your own visual output (convert PDF pages to PNG first). Descriptions can be wrong about small text and exact numbers; when you need exact values, take them from the underlying data if you can.
    - `file_path` (string, required): absolute path to an image in the pod.
    - `question` (string, required): exactly what you need to know.
    """
    return v_describe_image(file_path, question)


def share_file(file_path: str):
    """
    Make a file or directory available to the user; directories are sent as one zip. Files you don't share are invisible to the user. Share each deliverable in a reply before your final `respond`, and check the result to confirm it worked. Don't share bulky or irrelevant content (virtualenvs, `node_modules`, caches); copy deliverables into a clean folder first if needed.
    - `file_path` (string, required): absolute path to an existing file or directory.
    """
    container = get_container()
    quoted_path = shlex.quote(file_path)
    probe = container.exec(
        [
            "bash",
            "-c",
            f"if [ -f {quoted_path} ]; then echo file;"
            f" elif [ -d {quoted_path} ]; then echo dir;"
            f" else echo missing; fi",
        ]
    ).strip()
    if probe == "missing":
        return {"error": f"Path not found inside the pod: {file_path}"}
    return {
        "path": file_path,
        "name": basename(file_path.rstrip("/")) or file_path,
        "is_dir": probe == "dir",
    }


def _reset_browser():
    """Close the shared browser so the next web_interact starts a clean one.

    Called by the server when a chat is reset: the browser lives on the host
    (outside the pod), so it must be torn down separately from the VM.
    """
    global BROWSER
    if BROWSER is None:
        return "No browser session was open."
    browser, BROWSER = BROWSER, None
    try:
        browser.close()
    except Exception:
        get_logger().exception("Failed to close the browser during reset")
    return "Browser session closed."


def respond(text: str):
    """
    Send a message to the user and end your turn. Use it when the task is done and verified; when you need approval, a decision, or information only the user has; or when you're blocked after trying alternatives. Only a preceding `think` may share its reply. Markdown is supported.
    - `text` (string, required)
    """
    return text


TOOL_REGISTRY = {
    "think": think,
    "execute_command": execute_command,
    "read_file": read_file,
    "edit_file": edit_file,
    "web_search": web_search,
    "web_interact": web_interact,
    "describe_image": describe_image,
    "share_file": share_file,
    "respond": respond,
}
