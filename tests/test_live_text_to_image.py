"""Live one-shot check of the text_to_image tool: real API, real pod (manual run).

Run with:
    uv run python tests/test_live_text_to_image.py
"""

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image

from syna.capabilities import tools
from syna.state import get_container


def main() -> None:
    """Generate one image through the tool and verify the pod file with Pillow."""
    result = tools.text_to_image(
        "a simple red circle centered on a white background, minimalist flat style"
    )
    print("RESULT:", result)

    if not isinstance(result, str) or "Image saved to" not in result:
        raise SystemExit("LIVE TEST FAILED: tool returned an error")

    pod_path = re.search(r"/workspace/[\w./-]+", result).group(0)
    container = get_container()
    with tempfile.TemporaryDirectory(prefix="syna-live-") as temp_dir:
        container.copy_from(pod_path, temp_dir)
        local = Path(temp_dir, basename(pod_path))
        with Image.open(local) as img:
            img.load()
            print(
                f"VERIFY: {img.format} {img.size} mode={img.mode},"
                f" {local.stat().st_size // 1024} KB"
            )
    print("LIVE TEST PASS")


if __name__ == "__main__":
    main()
