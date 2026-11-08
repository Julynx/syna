"""The tools exposed to the model, and the whitelists that dispatch them.

TOOL_REGISTRY and BROWSER_COMMANDS are explicit whitelists: parse.py
dispatches through TOOL_REGISTRY instead of getattr on the module, and
the browser tool dispatches through BROWSER_COMMANDS instead of getattr on
the
Browser object. That keeps internals (editor primitives, path utilities,
browser internals like run()/page/close()) out of the model's reach.

Editing a docstring or the system prompt (assets/prompt.md) is expensive
and near-irreversible: every token is paid on every request, and text
accretes because additions are easy to justify and nobody dares remove
them. Before adding anything, apply three tests: does the model have to
know this? Will it matter over 50% of the time across a multitude of
tasks? Can the model learn it reliably from the tool's own output or
trial and error? If the output already carries the information at the
moment it matters, a note, an error message, a ref, the description
does not need it. Document how the implementation works in
docs/technical_description.md instead; docstrings carry only the contract
the model must know up front: parameters, units, and non-obvious
constraints that output cannot teach.

Every tool docstring becomes the model-facing tool documentation through
the request's tool schemas (`get_tool_definitions` in
protocol/tool_schemas.py), so the docstrings below must stay in sync
with actual behavior.
"""

import itertools
import re
import shlex
import tempfile
import time
from pathlib import Path
from posixpath import basename, dirname
from typing import Any

from bollard import DockerException

from .browser import (
    Browser,
    BrowserCommandTimeout,
    PWError,
    SharedBrowser,
    ToolError,
    describe_pw_error,
)
from ..config import (
    get_logger,
    load_browser_launch_timeout_s,
    load_command_timeout_s,
    load_output_limit_chars,
    load_tool_call_timeout_s,
    load_wait_for_timeout,
)
from .editor import edit_file as e_edit_file
from .editor import read_file as e_read_file
from .image_generation import (
    ImageGenerationError,
    generate_image,
    image_generation_enabled,
    load_image_settings,
)
from ..interaction import InteractionCancelled, InteractionError
from ..interaction import ask as ask_interactive
from .processes import (
    NONINTERACTIVE_ENV_EXPORTS,
    WAIT_FOR_MAX_S,
    WAIT_FOR_POLL_INTERVAL_S,
    check_process_state,
    format_process_report,
    kill_process,
    registry_for,
    spawn_process,
    unknown_id_error,
)
from .speech import (
    EXTENSION_BY_MEDIA_TYPE,
    MAX_TTS_INPUT_CHARS,
    SpeechError,
    audio_format_for_path,
    load_speech_settings,
    synthesize_speech,
    transcribe_audio,
    voice_enabled,
)
from ..state import (
    ToolCancelled,
    get_call_output_path,
    get_cancel_event,
    get_container,
    set_last_exit_code,
)
from .subagents import (
    check_subagent,
    kill_subagent,
    spawn_subagent,
    subagent_is_settled,
)
from .verification import run_verification as v_run_verification
from .vision import describe_image as v_describe_image
from .web_search import search

SHARED_BROWSER = SharedBrowser(
    lambda: Browser(),
    thread_name="syna-tool-browser-start",
    log_label="Tool browser",
)

_artifact_counter = itertools.count(1)

BROWSER_COMMANDS = {
    "visit": Browser.visit,
    "snapshot": Browser.snapshot,
    "click": Browser.click,
    "type": Browser.type,
    "select": Browser.select,
    "press": Browser.press,
    "scroll": Browser.scroll,
    "back": Browser.back,
    "read": Browser.read,
    "screenshot": Browser.screenshot,
}

EXIT_CODE_MARKER_PATTERN = re.compile(r"\n<<<SYNA_EXIT=(\d+)>>>\s*$")
COMMAND_TIMEOUT_MARGIN_S = 5
COMMAND_KILL_GRACE_S = 5
COMMAND_TIMEOUT_EXIT_CODES = {124, 137}


def command_runtime_limit_s() -> int:
    """The effective maximum runtime of one execute_command call.

    The configured command timeout bounded by the harness's tool-call
    detach timeout; Allegro's halving applies through the thread-local
    timeout scale, so callers on a turn thread get the value actually
    enforced for that turn.
    """
    return _effective_command_timeout()


def _command_timeout_notice(exit_code: int, timeout_s: int) -> str:
    """Explain a timeout-killed command to the model, with the exact limit."""
    if exit_code == 137:
        what = (
            "was killed (exit code 137): either the final kill after the"
            " timeout, or the system killed it (for example, out of memory)"
        )
    else:
        what = "hit the timeout and was killed (exit code 124)"
    return (
        f"\n\nTIMEOUT: this execute_command call failed. The command {what}"
        f" after {timeout_s} seconds; {timeout_s} seconds is the maximum"
        " runtime of one execute_command call. The command is no longer"
        " running and the output above is partial. For longer work, use"
        " the process tool to run it detached: it keeps running, its"
        " output streams to files you can check, and a notice arrives"
        " when it finishes."
    )


def think(thought: str):
    """
    Record reasoning in the conversation. It does not affect the pod.

    Your private reasoning may not carry over between turns, but `think` entries stay visible for the rest of the task. Use it for things you will need later: a plan for multi-step work, a conclusion from a surprising or failed result, or a check of your claims against the evidence before your final answer.

    Call `think` alongside other tool calls when planning multi-step work. This is not a message to the user.

    - `thought` (string, required): The note to record.
    """
    return "(noted)"


def execute_command(command: str, arguments: list[str] | None = None):
    """
    Run a command in the pod (behavior described under Environment).
    - `command` (string, required): a full bash command line. Pipes, redirection, `&&`, and quoting work.
    - `arguments` (list of strings, optional): if present, `command` must be a bare executable name, and these are its arguments, passed without shell processing. Useful when quoting gets awkward.

    One call's runtime is capped (a killed call reports the exact limit); a failed or timed-out call skips the remaining calls of the same reply, so run calls that may approach the cap alone.

    To create a file, use a quoted heredoc so the shell expands nothing: `cat > /workspace/report/build.py <<'EOF'` … `EOF`.

    Example: execute_command(command="cd /workspace/app && python3 -m pytest -q 2>&1 | tail -n 30")
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
            f"chmod 444 {shlex.quote(output_path)} 2>/dev/null || true",
        ]
    )
    container.exec(["bash", "-c", runner_script])
    view = container.exec(["bash", "-c", _reader_script(output_path, exit_path)])

    match = EXIT_CODE_MARKER_PATTERN.search(view)
    if match:
        exit_code = int(match.group(1))
        set_last_exit_code(exit_code)
        view = view[: match.start()]
        if exit_code in COMMAND_TIMEOUT_EXIT_CODES:
            view += _command_timeout_notice(exit_code, timeout_s)
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
        f"  head -c $((limit / 2)) {quoted_output}\n"
        "  printf '\\n[... %s bytes omitted; full output in %s ...]\\n'"
        f' "$((size - limit))" {quoted_output}\n'
        f"  tail -c $((limit / 2)) {quoted_output}\n"
        "fi\n"
        f"rm -f {quoted_exit}\n"
        'printf "\\n<<<SYNA_EXIT=%s>>>\\n" "$code"'
    )


def web_search(query: str):
    """
    Search the web. Returns the top 10 results with title, description, and URL. Snippets can be outdated or out of context: visit the page before relying on a claim. To open a page, use the `browser` tool with command `visit`.

    - `query` (string, required): what to search for.

    Example: web_search(query="debian 13 release notes")
    """
    return search(query)


def _write_into_pod(data: bytes, pod_dir: str, file_name: str) -> str:
    """Write bytes into the pod at pod_dir/file_name and return the path.

    Artifacts (screenshots, transcripts, audio, generated images) are
    evidence and deliverables, not scratch space: each file is made
    read-only after the copy so the model cannot silently overwrite a
    result already reported. Regeneration produces new, timestamped names.
    """
    container = get_container()
    container.exec(["bash", "-c", f"mkdir -p {shlex.quote(pod_dir)}"])
    with tempfile.TemporaryDirectory(prefix="syna-artifact-") as temp_dir:
        host_file = Path(temp_dir, file_name)
        host_file.write_bytes(data)
        container.copy_to(str(host_file), pod_dir)
    pod_path = f"{pod_dir}/{file_name}"
    try:
        container.exec(["bash", "-c", f"chmod 444 {shlex.quote(pod_path)}"])
    except Exception:
        get_logger().warning("Could not make %s read-only", pod_path, exc_info=True)
    return pod_path


def _capture_screenshot(screenshot_fn, browser, args: dict) -> str:
    """Capture a viewport screenshot and copy it into the pod.

    The image lands in /workspace/screenshots so describe_image can read it
    and share_file can deliver it to the user. Browser-side failures follow
    the same 'ERROR:' convention as the other browser commands, so the batch
    keeps running; infrastructural failures (pod copy) still propagate.
    """
    try:
        data = screenshot_fn(browser, **args)
    except PWError as exc:
        return "ERROR: " + describe_pw_error(exc)
    except ToolError as exc:
        return f"ERROR: {exc}"
    except TypeError as exc:
        return f"ERROR: bad arguments for screenshot: {exc}"
    name = "screenshot-{}-{:04d}.png".format(
        time.strftime("%Y%m%d-%H%M%S"), next(_artifact_counter)
    )
    pod_path = _write_into_pod(data, "/workspace/screenshots", name)
    return (
        f"Screenshot saved to {pod_path} ({max(1, len(data) // 1024)} KB)."
        " The screenshot is capped to the viewport: if something is not"
        " visible, it may be out of view, scroll and take another screenshot"
        " to see it."
    )


def browser(command: str, args: dict[str, Any]):
    """
    Visit a page or control a persistent browser that runs JavaScript; the current page, cookies, and logins persist between calls. Use `visit` to open and read a page (cookie banners are dismissed and lazy-loaded content is triggered automatically); add `read`, `click`, `type`, and the other commands when a page needs interaction. Use `execute_command` (`curl`, `wget`) to download files and call APIs.

    - `command` (string, required): browser command to run; see the list below.
    - `args` (object, required): parameters for that command; see the list below.

    * `visit`: `url` (string, required). Opens a page and returns a compact view. Interactive elements appear like `[12] button "Buy"`; pass the number as `ref`. Anti-bot challenges are waited out; if one persists, the view shows it.
    * `snapshot`: `viewport_only` (boolean), `full` (boolean). Re-reads the current page.
    * `click`: `ref` (integer, required). Returns what changed.
    * `type`: `ref` (integer, required), `text` (string, required), `clear` (boolean, default true), `submit` (boolean; presses Enter after typing).
    * `select`: `ref` (integer, required), `option` (string, required): visible label or value.
    * `press`: `key` (string, required), e.g. `Enter`, `Escape`, `Tab`, `ArrowDown`.
    * `scroll`: `direction` (`down`, `up`, `top`, or `bottom`), `amount` (fraction of viewport height, default 0.8). Returns the visible viewport.
    * `back`: no arguments.
    * `read`: `ref` (integer; omit for the whole page), `max_chars` (integer). Full plain text, for when a view truncated something you need. Tables come back one line per row with cells separated by ` | `.
    * `screenshot`: no arguments. Takes a screenshot of the current viewport, saves it into the pod, and returns its path. You may inspect it with `describe_image` if you need to, and share it with `share_file` if the user wants to see it. Use it when the user asks for a screenshot or when a look at the page would help you debug a browser task.

    Refs belong to the page state they came from; after navigation or large page changes, use refs from the newest output. Submitting forms that send, post, or buy something needs approval (see Protecting the user).

    Example: browser(command="type", args={"ref": 7, "text": "debian 13 release notes", "submit": True})
    """
    if not args:
        args = {}

    function = BROWSER_COMMANDS.get(command)
    if function is None:
        set_last_exit_code(1)
        return (
            f"ERROR: unknown browser command '{command}'. Available commands:"
            f" {', '.join(sorted(BROWSER_COMMANDS))}"
        )
    browser_instance = SHARED_BROWSER.get(load_browser_launch_timeout_s())
    try:
        if command == "screenshot":
            view = _capture_screenshot(function, browser_instance, args)
        else:
            view = function(browser_instance, **args)
    except ToolCancelled:
        _discard_browser_session("the harness stopped a browser command")
        raise
    except BrowserCommandTimeout:
        _discard_browser_session(
            "a timed-out browser command left the session in an unknown state"
        )
        set_last_exit_code(1)
        raise
    if isinstance(view, str) and view.startswith("ERROR:"):
        set_last_exit_code(1)
    return view


def _copy_from_pod(container, pod_path: str, host_dir: Path) -> str | None:
    """Copy a file out of the pod; report a missing path as a clean error.

    Returns None on success, or an error message (with the call's exit code
    set to 1) when the Docker API reports the pod path as absent. Any other
    Docker failure propagates unchanged.
    """
    try:
        container.copy_from(pod_path, str(host_dir))
    except DockerException as exc:
        message = str(exc)
        if (
            message.startswith("Docker API Error (404)")
            and "no such file" in message.lower()
        ):
            set_last_exit_code(1)
            return f"No such file or directory: {pod_path}"
        raise
    return None


def read_file(file_path: str, start: int = 1, limit: int = 200):
    """
    Read a text file with line numbers. Prefer this to `cat`.
    - `file_path` (string, required): absolute path.
    - `start` (integer, required): first line to read, counting from 1.
    - `limit` (integer, required): number of lines. Keep it small; in large files, find the relevant lines first with `grep -n`.

    The numbers shown are the file's real line numbers; use them with `edit_file`.

    Example: read_file(file_path="/workspace/app/main.py", start=1, limit=80)
    """
    with tempfile.TemporaryDirectory(prefix="syna-") as temp_dir:
        temp_path = Path(temp_dir)
        container = get_container()
        error = _copy_from_pod(container, file_path, temp_path)
        if error:
            return error
        output = e_read_file(Path(temp_path, basename(file_path)), start, limit)
    return output


def edit_file(file_path: str, operations: list[dict]):
    """
    Edit a text file by line number or by exact string match. Prefer this to `sed` or rewriting the whole file.
    - `file_path` (string, required): absolute path.
    - `operations` (list, required): one or more of
      - `{"insert_above": {"line_num": 3, "insert_lines": ["..."]}}`
      - `{"replace": {"line_num_or_range": [45, 47], "replace_with_lines": ["..."]}}`
      - `{"delete": {"line_num_or_range": [45, 47]}}`
      - `{"replace_string": {"old_string": "...", "new_string": "..."}}`: replaces the one exact occurrence of old_string; fails unless it matches exactly once. Prefer this for small edits: it cannot hit the wrong line.

    Ranges are inclusive. Every line number in a call refers to the file as it was before that call, so several operations can be combined without recalculating; line numbers count lines exactly like `grep -n`. The result includes a unified diff of what changed, so you can confirm the edit landed as intended without re-reading. Afterwards, numbering has shifted: re-read the affected lines before the next edit. Write each line with its full indentation.

    Example: edit_file(file_path="/workspace/app/config.py", operations=[{"replace_string": {"old_string": "TIMEOUT_SECONDS = 10", "new_string": "TIMEOUT_SECONDS = 30"}}])
    """
    with tempfile.TemporaryDirectory(prefix="syna-") as temp_dir:
        temp_path = Path(temp_dir)
        container = get_container()
        error = _copy_from_pod(container, file_path, temp_path)
        if error:
            return error
        temp_file_path = Path(temp_path, basename(file_path))
        diff = e_edit_file(temp_file_path, operations)
        container.copy_to(str(temp_file_path), dirname(file_path) or "/")
    message = f"File edited successfully ({len(operations)} operations applied)."
    if diff:
        message += f"\n\nApplied diff:\n{diff}"
    return message


def describe_image(file_path: str, question: str):
    """
    Get a text description of an image file, focused on your question. To create or edit images, call text_to_image or script the file. Useful for checking your own visual output (convert to PNG first). Descriptions can be wrong about small text and exact numbers; when you need exact values, take them from the underlying data if you can. Large images are downscaled before analysis; if you need to inspect fine detail (small text, icons, flags), pass a zoomed crop of that region instead of a full-page render.
    - `file_path` (string, required): absolute path to an image file in the pod.
    - `question` (string, required): exactly what you need to know about the image.
    """
    return v_describe_image(file_path, question)


def speech_to_text(file_path: str, language: str | None = None):
    """
    Transcribe an audio file into written text. Call this tool to turn a recording, voice note, or audio clip in the pod into text you can work with. Do not call this tool when you can easily obtain the transcript by other means. For example, for YouTube videos, install yt-dlp and run `yt-dlp --skip-download --write-subs --sub-langs en --convert-subs srt "VIDEO_URL"` (or `--write-auto-subs` if the video has no real subtitles) instead of calling this tool.
    - `file_path` (string, required): absolute path to an audio file in the pod; supported formats are wav, mp3, m4a, ogg, opus, flac, aac, and webm.
    - `language` (string, optional): ISO-639-1 code of the audio's language, e.g. `en` or `de`. Omit it to let the model detect the language; auto-detection is not always supported and can hurt accuracy.

    The full transcript is saved as a text file in /workspace/transcripts and its text is returned. Transcription quality drops with heavy background noise; the transcript may mishear names and numbers.
    """
    settings = load_speech_settings()
    if not voice_enabled():
        return {
            "error": (
                "Voice is disabled by the user (settings, Voice category)."
                " You did nothing wrong; tell the user they can enable it"
                " there if they want dictation."
            )
        }
    if not settings.stt_available:
        return {
            "error": (
                "Speech transcription is not available on this endpoint;"
                " it requires an OpenRouter endpoint."
            )
        }
    with tempfile.TemporaryDirectory(prefix="syna-speech-") as temp_dir:
        temp_path = Path(temp_dir)
        container = get_container()
        error = _copy_from_pod(container, file_path, temp_path)
        if error:
            return {"error": error}
        source_path = temp_path / basename(file_path)
        audio_format = audio_format_for_path(source_path.name)
        if audio_format is None:
            return {
                "error": (
                    f"Unsupported audio file '{file_path}'. Supported formats:"
                    " wav, mp3, m4a, ogg, opus, flac, aac, webm."
                )
            }
        try:
            transcript = transcribe_audio(
                source_path.read_bytes(), audio_format, settings, language
            )
        except SpeechError as exc:
            return {"error": str(exc)}
    name = "transcript-{}-{:04d}.txt".format(
        time.strftime("%Y%m%d-%H%M%S"), next(_artifact_counter)
    )
    pod_path = _write_into_pod(
        transcript.encode("utf-8"), "/workspace/transcripts", name
    )
    limit = load_output_limit_chars()
    if len(transcript) <= limit:
        return f"{transcript}\n\n[Transcript saved to {pod_path}]"
    return (
        f"{transcript[:limit]}"
        f"... [transcript truncated; the full text is saved to {pod_path}]"
    )


def text_to_speech(file_path: str):
    """
    Synthesize spoken audio from a text file. Call it when the user wants an audio version of a document.
    - `file_path` (string, required): absolute path to a text file in the pod.

    The audio is saved into /workspace/audio; deliver it to the user with `share_file`. A request may hold at most 4000 characters; split longer files into parts of at most 4000 characters and synthesize each part, then share all the audio files. The audio is for listening, not for lossless transport: a round-trip through speech_to_text can drop words.
    """
    settings = load_speech_settings()
    if not voice_enabled():
        return {
            "error": (
                "Voice is disabled by the user (settings, Voice category)."
                " You did nothing wrong; tell the user they can enable it"
                " there if they want read-aloud."
            )
        }
    if not settings.tts_available:
        return {
            "error": (
                "Speech synthesis is not available on this endpoint;"
                " it requires an OpenRouter endpoint."
            )
        }
    with tempfile.TemporaryDirectory(prefix="syna-speech-") as temp_dir:
        temp_path = Path(temp_dir)
        container = get_container()
        error = _copy_from_pod(container, file_path, temp_path)
        if error:
            return {"error": error}
        text = (temp_path / basename(file_path)).read_text(
            encoding="utf-8", errors="replace"
        )
    if len(text) > MAX_TTS_INPUT_CHARS:
        return {
            "error": (
                f"The file holds {len(text)} characters; one synthesis"
                f" request accepts at most {MAX_TTS_INPUT_CHARS}. Split the"
                f" text into parts of at most {MAX_TTS_INPUT_CHARS}"
                " characters and call this tool for each part."
            )
        }
    try:
        audio = synthesize_speech(text, settings)
    except SpeechError as exc:
        return {"error": str(exc)}
    extension = EXTENSION_BY_MEDIA_TYPE.get(audio.media_type, "wav")
    name = "speech-{}-{:04d}.{}".format(
        time.strftime("%Y%m%d-%H%M%S"), next(_artifact_counter), extension
    )
    pod_path = _write_into_pod(audio.content, "/workspace/audio", name)
    return (
        f"Speech audio saved to {pod_path} ({max(1, len(audio.content) // 1024)} KB)."
    )


def text_to_image(prompt: str):
    """
    Generates an image from a text description using generative AI, and saves it into the pod.

    Use this tool:
      * For original characters, creatures, scenes or locations that don't exist in the real world, when no online image would fit the request.
      * When the user explicitly asks for AI-generated or original art.
      * To depict a real-world subject, object or location creatively or artistically rather than exactly, and a script can't easily do it (e.g., the Eiffel Tower made of pizza).

    Do not use this tool:
      * To depict real-world events, people, places, species, animals, plants, products or objects as they exist in the world. Source an image from a reliable online source instead; accuracy prevails.
      * To assemble images and text into an infographic, a grid with labels, or similar structures. This tool cannot source images from the pod or the web, and cannot reliably generate structured documents like that. Source the base images and compose them using a script or markup rendered to an image instead.
      * For diagrams, charts, and other visualizations based on quantitative data. Use a script that parses such data and outputs the diagram instead of calling this tool.

    If this tool is unavailable or unsuitable, and images are still needed for the task, fall back to downloading from online sources or using a script.
    - `prompt` (string, required): description of the image to generate; mention subject, style, composition, lighting, and mood concretely.
    """
    settings = load_image_settings()
    if not image_generation_enabled():
        return {
            "error": (
                "Image generation is disabled by the user (settings,"
                " Images category) because it is expensive. You did"
                " nothing wrong; tell the user they can enable it there."
            )
        }
    if not settings.model:
        return {
            "error": (
                "Image generation is not available on this endpoint;"
                " it requires an OpenRouter endpoint."
            )
        }
    try:
        image = generate_image(prompt, settings)
    except ImageGenerationError as exc:
        return {"error": str(exc)}
    name = "generated-{}-{:04d}.{}".format(
        time.strftime("%Y%m%d-%H%M%S"), next(_artifact_counter), image.extension
    )
    pod_path = _write_into_pod(image.content, "/workspace/images", name)
    result = f"Image saved to {pod_path} ({max(1, len(image.content) // 1024)} KB)."
    if image.media_type != "image/svg+xml":
        result += " You may inspect it with the describe_image tool if you need to."
    return result + " Share it with the share_file tool."


def share_file(file_path: str):
    """
    Make a file or directory available to the user; directories are sent as one zip. Files you don't share are invisible to the user. Share each deliverable before finishing your turn. Don't share bulky or irrelevant content (virtualenvs, `node_modules`, caches); copy deliverables into a clean folder first if needed.
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
        set_last_exit_code(1)
        return f"Path not found inside the pod: {file_path}"
    return {
        "path": file_path,
        "name": basename(file_path.rstrip("/")) or file_path,
        "is_dir": probe == "dir",
    }


def ask_user(question_text: str, choices: list):
    """
    Ask the user a question and wait for their answer. The conversation pauses until they respond. Call this tool when you need information from the user to complete a task, and you cannot reasonably assume or deduce the answer yourself (a key fact is missing, or options compete and none is clearly better). Note: tool calls in the same response as this one might not execute until later.
    - `question_text` (string, required): the question itself, clear and self-contained.
    - `choices` (array of strings, required): short answer options to show, each a self-contained phrase. An "Other: Type your own response" option is always added automatically, so users can always answer outside your choices. Never include your own "Other" option. For open-ended questions, pass an empty array and a text field will be shown instead.
    """
    if not isinstance(question_text, str) or not question_text.strip():
        set_last_exit_code(1)
        return {"error": "question_text must be a non-empty string."}
    if not isinstance(choices, list) or not all(
        isinstance(choice, str) and choice.strip() for choice in choices
    ):
        set_last_exit_code(1)
        return {"error": "choices must be an array of non-empty strings."}
    try:
        answer = ask_interactive(question_text, choices)
    except InteractionCancelled:
        set_last_exit_code(1)
        raise
    except InteractionError as exc:
        set_last_exit_code(1)
        return {"error": f"{exc} The question was not delivered to the user."}
    return f"User's answer: {answer}"


def review_work(summary: str):
    """
    If the task was especially complicated and/or prone to error, call this tool on its own after the work is done to verify the results before reporting them to the user. An adversarial reviewer model inspects the conversation, and its report comes back as this call's result. No need to explain your methodology; the verifier gets that information by inspecting this conversation, including your tool calls.
    - `summary` (string, required): the final response exactly as you would have sent it to the user, excluding any files or artifacts.
    """
    if not isinstance(summary, str) or not summary.strip():
        set_last_exit_code(1)
        return {"error": "summary must be a non-empty string."}
    result = v_run_verification(summary)
    if isinstance(result, dict) and "error" in result:
        # A review that did not happen must not render as a green row.
        set_last_exit_code(1)
    return result


def process(command: str, args: dict | None = None):
    """
    Run a bash command in the pod as a detached background process: spawn returns immediately and the process keeps running while you do other work; a notice arrives here automatically when it finishes. Use it for long-running work (servers, builds, big downloads, benchmark runs) that would outlive one execute_command call.
    - `command` (string, required): `spawn`, `check`, or `kill`.
    - `args` (object, required): parameters for that command; see the list below.

    * `spawn`: `command` (string, required): the bash command to run; `name` (string, optional): a short id of your choice (letters, digits, dots, hyphens); otherwise one like process_0001 is assigned. Its stdout and stderr stream to /workspace/processes/{id}/stdout and stderr, and it is killed when it exceeds its runtime cap (exit code 124). Processes die with the pod.
    * `check`: `process_id` (string, required). Returns state, running time, and the last characters of stdout and stderr.
    * `kill`: `process_id` (string, required). Kills the whole process tree; the output files stay.

    Example: process(command="spawn", args={"command": "python3 -m http.server 8080", "name": "server"})
    """
    args = args or {}
    if command == "spawn":
        spawn_command = args.get("command")
        if not isinstance(spawn_command, str) or not spawn_command.strip():
            set_last_exit_code(1)
            return (
                "ERROR: process spawn needs 'command': the bash command"
                " to run, as a non-empty string."
            )
        result = spawn_process(spawn_command, name=args.get("name"))
        if "error" in result:
            set_last_exit_code(1)
            return "ERROR: " + result["error"]
        return result
    if command in ("check", "kill"):
        process_id = args.get("process_id")
        if not isinstance(process_id, str) or not process_id.strip():
            set_last_exit_code(1)
            return (
                f"ERROR: process {command} needs 'process_id': the id"
                " the spawn returned."
            )
        if command == "check":
            result = _check_process(process_id)
        else:
            result = kill_process(process_id, by="model")
        if result.startswith("ERROR:"):
            set_last_exit_code(1)
        return result
    set_last_exit_code(1)
    return (
        f"ERROR: unknown process command '{command}'. Available commands:"
        " check, kill, spawn"
    )


def _check_process(process_id: str) -> str:
    """Fresh state probe plus tails for one registered process."""
    container = get_container()
    registry = registry_for(container)
    with registry.lock:
        proc = registry.processes.get(process_id)
    if proc is None:
        set_last_exit_code(1)
        return "ERROR: " + unknown_id_error(process_id)
    try:
        state, stdout, stderr = check_process_state(container, proc.dir)
    except Exception as exc:
        set_last_exit_code(1)
        return f"ERROR: could not read the process output: {exc}"
    return format_process_report(proc, state, stdout, stderr)


def subagent(command: str, args: dict | None = None):
    """
    Delegate a self-contained task to a background subagent: a fresh instance of you with the same prompt, tools, and pod, running detached. Spawn returns immediately; a notice arrives here automatically when the subagent finishes. Use it for parallelizable or long-running subtasks; do the work yourself when later steps depend on your intermediate results. Subagents cannot ask the user questions, only a fixed number run at once, and a subagent cannot spawn subagents beyond the depth cap.
    - `command` (string, required): `spawn`, `check`, or `kill`.
    - `args` (object, required): parameters for that command; see the list below.

    * `spawn`: `goal` (string, required): the task, self-contained and verifiable, naming its deliverable paths; `name` (string, optional): a short id of your choice; otherwise one like subagent_0001. It shares the pod with you: partition file work by path so concurrent subagents cannot overwrite each other.
    * `check`: `subagent_id` (string, required). Returns state, running time, the tail of its message log, and its transcript path.
    * `kill`: `subagent_id` (string, required). Stops it at its next pause; its transcript and any response stay in /workspace/subagents/{id}/.

    Example: subagent(command="spawn", args={"goal": "Read every paper in /workspace/papers and write /workspace/related.md summarizing them", "name": "lit-review"})
    """
    args = args or {}
    if command == "spawn":
        goal = args.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            set_last_exit_code(1)
            return (
                "ERROR: subagent spawn needs 'goal': the task for the"
                " subagent, as a non-empty string."
            )
        result = spawn_subagent(goal, name=args.get("name"))
        if "error" in result:
            set_last_exit_code(1)
            return "ERROR: " + result["error"]
        return result
    if command in ("check", "kill"):
        subagent_id = args.get("subagent_id")
        if not isinstance(subagent_id, str) or not subagent_id.strip():
            set_last_exit_code(1)
            return (
                f"ERROR: subagent {command} needs 'subagent_id': the id"
                " the spawn returned."
            )
        if command == "check":
            result = check_subagent(subagent_id)
        else:
            result = kill_subagent(subagent_id, by="model")
        if result.startswith("ERROR:"):
            set_last_exit_code(1)
        return result
    set_last_exit_code(1)
    return (
        f"ERROR: unknown subagent command '{command}'. Available commands:"
        " check, kill, spawn"
    )


def wait_for(id: str, max_wait_s: int | None = None):
    """
    Block until a background process or subagent finishes or is stopped, or until the wait times out. On timeout it reports "still running"; a notice arrives here automatically when it finishes anyway, so waiting again is optional and doing other work meanwhile is usually better.
    - `id` (string, required): the process or subagent id.
    - `max_wait_s` (integer, optional): seconds to wait at most; default 120.

    Example: wait_for(id="build", max_wait_s=600)
    """
    if (
        not isinstance(max_wait_s, int)
        or isinstance(max_wait_s, bool)
        or max_wait_s <= 0
    ):
        if max_wait_s is not None:
            set_last_exit_code(1)
            return "ERROR: max_wait_s must be a positive number of seconds."
        max_wait_s = load_wait_for_timeout()
    budget = min(max_wait_s, WAIT_FOR_MAX_S)
    container = get_container()
    registry = registry_for(container)
    entry = registry.lookup(id)
    if entry is None:
        set_last_exit_code(1)
        return "ERROR: " + unknown_id_error(id)
    kind, record = entry
    deadline = time.monotonic() + budget
    while True:
        entry = registry.lookup(id)
        if entry is None:
            set_last_exit_code(1)
            return "ERROR: " + unknown_id_error(id)
        kind, record = entry
        if kind == "process":
            if record.state != "running":
                return _finished_process_wait(record, container)
        else:
            if subagent_is_settled(record):
                return check_subagent(record.id, registry, container)
        cancel_event = get_cancel_event()
        if cancel_event is not None and cancel_event.is_set():
            raise ToolCancelled()
        if time.monotonic() >= deadline:
            runtime = time.time() - record.started
            return (
                f"'{id}' is still running after waiting {budget:.0f}s"
                f" (total runtime {runtime:.0f}s). It keeps running and a"
                " notice arrives here automatically when it finishes. You"
                " can wait again, check its current output"
                f" (process check / subagent check), or do other work"
                " meanwhile."
            )
        time.sleep(WAIT_FOR_POLL_INTERVAL_S)


def _finished_process_wait(proc, container) -> str:
    """The wait_for result when the waited process has ended."""
    try:
        state, stdout, stderr = check_process_state(container, proc.dir)
    except Exception as exc:
        return f"Process '{proc.id}' ended, but its output is unreadable: {exc}"
    return (
        format_process_report(proc, state, stdout, stderr)
        + "\nIt finished during this wait."
    )


def _discard_browser_session(reason: str) -> None:
    """Drop the shared tool browser after a command was abandoned mid-flight.

    Reset-on-cancel: a command cancelled mid-flight leaves the shared
    session's page in an unknown state that a later command could land
    on. The teardown runs off the timed path, and the next browser call
    starts a clean browser through the launch budget.
    """
    if SHARED_BROWSER.invalidate():
        get_logger().info("Discarded the tool browser session: %s.", reason)


def _reset_browser():
    """Close the shared browser so the next browser call starts a clean one.

    Called by the server when a chat is reset: the browser lives on the host
    (outside the pod), so it must be torn down separately from the VM. The
    teardown runs in a helper thread: a session that wedged mid-command
    cannot be trusted to close promptly, and the reset must not block.
    """
    if SHARED_BROWSER.invalidate():
        return "Browser session closed."
    return "No browser session was open."


TOOL_REGISTRY = {
    "think": think,
    "execute_command": execute_command,
    "read_file": read_file,
    "edit_file": edit_file,
    "web_search": web_search,
    "browser": browser,
    "describe_image": describe_image,
    "speech_to_text": speech_to_text,
    "text_to_speech": text_to_speech,
    "text_to_image": text_to_image,
    "share_file": share_file,
    "ask_user": ask_user,
    "review_work": review_work,
    "process": process,
    "subagent": subagent,
    "wait_for": wait_for,
}
