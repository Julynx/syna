"""Assemble src/syna/assets/chat.html from the sources in webui/.

chat.html is a GENERATED file — edit the css/ and js/ sources in webui/,
then rebuild with: python scripts/build.py

webui/index.html is the page skeleton; every {{CSS}} marker is replaced in
order with a <style> block from webui/css/, and every {{JS}} marker with an
inline <script> block from webui/js/. The block order is fixed below.
"""

from pathlib import Path

CSS_FILES = [
    "base.css",
    "ambient.css",
    "chrome.css",
    "chat.css",
    "composer.css",
    "voice.css",
]
JS_FILES = ["ambient.js", "sfx.js", "markdown.js", "chat.js", "voice.js"]
SETUP_CSS_FILES = ["base.css", "setup.css"]
SETUP_JS_FILES = ["setup.js"]

TARGETS = [
    ("index.html", CSS_FILES, JS_FILES, "chat.html"),
    ("index-setup.html", SETUP_CSS_FILES, SETUP_JS_FILES, "setup.html"),
]


def _fill(build_dir: Path, html: str, css_files: list[str], js_files: list[str]) -> str:
    css_markers = html.count("{{CSS}}")
    if css_markers != len(css_files):
        raise SystemExit(
            f"Expected {len(css_files)} {{{{CSS}}}} markers, found {css_markers}"
        )
    js_markers = html.count("{{JS}}")
    if js_markers != len(js_files):
        raise SystemExit(
            f"Expected {len(js_files)} {{{{JS}}}} markers, found {js_markers}"
        )
    for name in css_files:
        source = (build_dir / "css" / name).read_text(encoding="utf-8")
        source = source.rstrip("\n") + "\n"
        html = html.replace("{{CSS}}", "<style>\n" + source + "\n</style>", 1)
    for name in js_files:
        source = (build_dir / "js" / name).read_text(encoding="utf-8")
        source = source.rstrip("\n") + "\n"
        html = html.replace("{{JS}}", "<script>\n" + source + "\n</script>", 1)
    return html


def build() -> None:
    root = Path(__file__).resolve().parent.parent
    build_dir = root / "webui"
    for skeleton, css_files, js_files, target_name in TARGETS:
        html = _fill(
            build_dir,
            (build_dir / skeleton).read_text(encoding="utf-8"),
            css_files,
            js_files,
        )
        target = root / "src" / "syna" / "assets" / target_name
        target.write_text(html, encoding="utf-8")
        print(f"Built {target} ({len(html)} bytes)")


if __name__ == "__main__":
    build()
