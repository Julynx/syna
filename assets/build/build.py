#!/usr/bin/env python3
"""Build the single-file chat.html from the split sources.

The syna-server serves exactly one GUI file (it reads assets/chat.html,
injects window.SYNA_CONFIG before </head>, and returns it). It has no
routes for css/ or js/, so the multi-file layout can't be served as-is.

Expected layout (sources live in this "build" folder):

    build/
      css/  js/
      build.py
      chat.html        <- shell (input, references css/ and js/)
    chat.html          <- OUTPUT: this file is overwritten in place

The shell's css/*.css and js/*.js tags are replaced with inline content at
their exact positions; vendor/markdown-it.min.js is left external (the
server already serves it, same as the original file).

Usage:  python build.py          (from this directory, or anywhere)
Stdlib only — no dependencies.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHELL = HERE / "chat.html"
OUT = HERE.parent / "chat.html"

LINK_RE = re.compile(r'<link rel="stylesheet" href="(css/[^"]+)">')
SCRIPT_RE = re.compile(r'<script src="(js/[^"]+)"></script>')


def fail(msg: str) -> None:
    print("BUILD FAILED:", msg)
    sys.exit(1)


def main() -> None:
    html = SHELL.read_text(encoding="utf-8")

    # ---- inline stylesheets --------------------------------------------
    def style_sub(m: re.Match) -> str:
        rel = m.group(1)
        path = HERE / rel
        if not path.is_file():
            fail(f"missing stylesheet referenced by shell: {rel}")
        css = path.read_text(encoding="utf-8")
        if "</style" in css.lower():
            fail(f"{rel} contains </style> and cannot be inlined")
        return f"<style>\n/* ==== {rel} ==== */\n{css}\n</style>"

    html, n_css = LINK_RE.subn(style_sub, html)

    # ---- inline scripts --------------------------------------------------
    def script_sub(m: re.Match) -> str:
        rel = m.group(1)
        path = HERE / rel
        if not path.is_file():
            fail(f"missing script referenced by shell: {rel}")
        js = path.read_text(encoding="utf-8")
        low = js.lower()
        if "</script" in low:
            fail(f"{rel} contains </script> and cannot be inlined")
        if "<!--" in js:
            fail(f"{rel} contains <!-- which can break inline <script> parsing")
        return f"<script>\n/* ==== {rel} ==== */\n{js}\n</script>"

    html, n_js = SCRIPT_RE.subn(script_sub, html)

    # ---- sanity checks ---------------------------------------------------
    if n_css == 0 or n_js == 0:
        fail(f"no tags matched (css={n_css}, js={n_js}) — shell changed? update the regexes")
    for pat, what in ((LINK_RE, "css <link>"), (SCRIPT_RE, "js <script>")):
        if pat.search(html):
            fail(f"unreplaced {what} tag left in output")
    if html.count("</head>") != 1:
        fail("output must contain exactly one </head> (server injects SYNA_CONFIG there)")
    if 'src="vendor/markdown-it.min.js"' not in html:
        fail("vendor/markdown-it.min.js tag is missing — it must stay external")

    banner = (
        "<!-- GENERATED FILE — edit the css/ and js/ sources in build/, "
        "then rebuild with: python build.py -->\n"
    )
    html = html.replace("<!DOCTYPE html>\n", "<!DOCTYPE html>\n" + banner, 1)

    OUT.write_text(html, encoding="utf-8")

    print(f"inlined {n_css} stylesheets + {n_js} scripts")
    print(f"wrote {OUT} ({len(html):,} bytes)")
    print("done — the chat.html next to build/ is ready to serve (assets/chat.html)")


if __name__ == "__main__":
    main()
