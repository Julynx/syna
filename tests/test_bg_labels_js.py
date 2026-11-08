"""Runs the JavaScript background-label suite through Node.js.

The label helpers are browser code (webui/js/bg_labels.js) tested by
tests/js/test_bg_labels.cjs, a zero-dependency Node script. This
wrapper wires that suite into the project's plain-script convention:
`python tests/test_bg_labels_js.py`. When Node.js is not installed
the suite reports a skip and succeeds, so contributors without Node
can still run every other suite.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "tests" / "js" / "test_bg_labels.cjs"


def main() -> None:
    """Run the Node suite and mirror its result on this process."""
    node = shutil.which("node")
    if node is None:
        print("SKIP test_bg_labels_js (node is not installed)")
        return
    completed = subprocess.run(
        [node, str(SUITE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        print("FAIL test_bg_labels_js")
        sys.exit(completed.returncode)
    print("PASS test_bg_labels_js")


if __name__ == "__main__":
    main()
