"""Import-and-resolve guard for the worlds benchmark runner.

The benchmark itself is expensive and deliberately wired into no test
loop; this suite only imports the runner module and resolves its
configuration paths. That catches refactors (moved functions, changed
config mechanisms) the runner would otherwise hit mid-launch, after
someone has already decided to pay for a run. It never creates a
container, a session, or a model call.
"""

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.config import load_config
from syna.paths import get_data_home

RUNNER_PATH = (
    Path(__file__).resolve().parent.parent / "evals" / "run_worlds_benchmark.py"
)


def _load_runner():
    """Import the runner module from its file without invoking main()."""
    spec = importlib.util.spec_from_file_location("run_worlds_benchmark", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_module_imports_cleanly():
    module = _load_runner()
    assert module.PROMPT
    assert module.DEFAULT_TIMEOUT_S > 0
    assert module.DEFAULT_MAX_STEPS > 0


def test_project_log_path_resolves_under_the_data_home():
    module = _load_runner()
    expected = get_data_home() / load_config().get("log_file", "syna.log")
    assert module._project_log_path() == expected


def main():
    from _runner import run_suite

    run_suite(
        [
            test_runner_module_imports_cleanly,
            test_project_log_path_resolves_under_the_data_home,
        ]
    )


if __name__ == "__main__":
    main()
