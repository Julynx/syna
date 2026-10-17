"""Assemble assets/chat.html from the sources in build/.

chat.html is a GENERATED file — edit the css/ and js/ sources in build/,
then rebuild with: python build.py

build/index.html is the page skeleton; every {{CSS}} marker is replaced in
order with a <style> block from build/css/, and every {{JS}} marker with an
inline <script> block from build/js/. The block order is fixed below.
"""

from pathlib import Path

CSS_FILES = ["base.css", "ambient.css", "chrome.css", "chat.css", "composer.css"]
JS_FILES = ["ambient.js", "sfx.js", "markdown.js", "chat.js"]


def build() -> None:
    root = Path(__file__).resolve().parent
    build_dir = root / "build"
    html = (build_dir / "index.html").read_text(encoding="utf-8")

    for name in CSS_FILES:
        source = (build_dir / "css" / name).read_text(encoding="utf-8")
        source = source.rstrip("\n") + "\n"  # files end with one newline; the
        # block adds its own blank line before </style>, matching the original
        html = html.replace("{{CSS}}", "<style>\n" + source + "\n</style>", 1)

    for name in JS_FILES:
        source = (build_dir / "js" / name).read_text(encoding="utf-8")
        source = source.rstrip("\n") + "\n"
        html = html.replace("{{JS}}", "<script>\n" + source + "\n</script>", 1)

    if "{{CSS}}" in html or "{{JS}}" in html:
        raise SystemExit(
            "Unfilled {{CSS}}/{{JS}} markers remain;"
            " check the CSS_FILES/JS_FILES lists against build/index.html"
        )

    target = root / "assets" / "chat.html"
    target.write_text(html, encoding="utf-8")
    print(f"Built {target} ({len(html)} bytes)")


if __name__ == "__main__":
    build()
