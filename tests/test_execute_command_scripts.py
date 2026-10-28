"""Smoke test for the pod-side shell scripts built by execute_command.

Runs the exact runner/reader scripts through a local bash to validate
quoting, exit-code capture, truncation, and the marker protocol without
a running container.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.capabilities import tools

WINDOWS_GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")


def find_bash() -> str | None:
    """Locate a real bash for the runner scripts.

    On Windows, `shutil.which("bash")` resolves to WSL's
    system32\\bash.exe, a Linux interop launcher that cannot run these
    scripts reliably; the scripts need Git Bash's userland. The git
    executable may be a shim (scoop) whose parent is not the git root,
    so the standard install locations are also probed.
    """
    if sys.platform != "win32":
        return shutil.which("bash")
    candidates = [Path.home() / "scoop/apps/git/current/bin/bash.exe", WINDOWS_GIT_BASH]
    git_exe = shutil.which("git")
    if git_exe:
        candidates.insert(0, Path(git_exe).parent.parent / "bin" / "bash.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


BASH = find_bash()


def run_scripts(
    command: str, exit_code_expected: int, timeout_s: int | None = None
) -> str:
    with tempfile.TemporaryDirectory(prefix="syna-exec-smoke-") as pod:
        out_path = str(Path(pod, "0001_execute_command.txt")).replace("\\", "/")
        exit_path = out_path + ".exit"
        import shlex

        duration = timeout_s or tools._effective_command_timeout()
        runner = "\n".join(
            [
                f"cd '{pod}'",
                f"exec > {shlex.quote(out_path)} 2>&1",
                f"export {tools.NONINTERACTIVE_ENV_EXPORTS}",
                f"timeout -k {tools.COMMAND_KILL_GRACE_S} {duration} bash -c {shlex.quote(command)}",
                f'printf %s "$?" > {shlex.quote(exit_path)}',
            ]
        )
        script_file = Path(pod, "_runner.sh")
        script_file.write_text(runner, encoding="utf-8", newline="\n")
        subprocess.run(
            [str(BASH), str(script_file).replace("\\", "/")], check=True, timeout=60
        )
        view = subprocess.run(
            [str(BASH), "-c", tools._reader_script(out_path, exit_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout

        match = tools.EXIT_CODE_MARKER_PATTERN.search(view)
        assert match, f"exit marker missing in: {view!r}"
        assert int(match.group(1)) == exit_code_expected, (
            f"exit code {match.group(1)} != {exit_code_expected}"
        )
        return view[: match.start()]


def main():
    if BASH is None:
        print("SKIP: no bash executable found")
        return

    view = run_scripts("echo hello world; echo root; exit 3", 3)
    assert "hello world" in view and "root" in view, view

    view = run_scripts("printf ''", 0)
    assert view.strip() == "(no output)", view

    big = "; ".join(f"echo line-{index}" for index in range(3000))
    view = run_scripts(big, 0)
    assert "characters omitted" not in view
    assert "line-0" in view and "line-2999" in view

    huge = "; ".join(f"echo {'x' * 60}-{index}" for index in range(1200))
    view = run_scripts(huge, 0)
    assert "bytes omitted" in view, view[:200]

    view = run_scripts("echo before; sleep 30; echo after", 124, timeout_s=3)
    assert "before" in view and "after" not in view, view

    print("ALL EXECUTE_COMMAND SHELL SCRIPT TESTS PASSED")


if __name__ == "__main__":
    main()
