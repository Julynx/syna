"""Build a static fixture page that previews the inline image download button.

Inlines the real webui CSS (build order) with font URLs rewritten to the
vendored font files, and replicates the exact DOM produced by chat.js:
bubble.model > markdown > inline-image-frame > img + copy-btn.image-download.

Run:  uv run python scripts/preview_inline_image.py
Then open the printed path in a browser.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS_DIR = ROOT / "webui" / "css"
FONT_DIR = ROOT / "src" / "syna" / "assets" / "vendor" / "fonts"
OUT_DIR = Path(
    r"C:\Users\Omega\AppData\Local\Temp\opencode\ui_preview"
)

CSS_FILES = [
    "base.css",
    "ambient.css",
    "chrome.css",
    "chat.css",
    "composer.css",
    "voice.css",
]

SVG_IMAGE = """<svg xmlns='http://www.w3.org/2000/svg' width='420' height='260'>
  <rect width='420' height='260' fill='#f4f1ea'/>
  <rect x='40' y='140' width='46' height='80' fill='#7ba05b'/>
  <rect x='106' y='100' width='46' height='120' fill='#5b8aa0'/>
  <rect x='172' y='60' width='46' height='160' fill='#c17b52'/>
  <rect x='238' y='120' width='46' height='100' fill='#8a6fa8'/>
  <rect x='304' y='80' width='46' height='140' fill='#a04f5b'/>
  <text x='210' y='36' font-family='sans-serif' font-size='18'
    text-anchor='middle' fill='#444'>Sample chart for the preview</text>
</svg>
"""

ICON_DOWNLOAD = (
    '<span class="material-symbols-outlined i-download">download</span>'
    '<span class="material-symbols-outlined i-check">check</span>'
)
ICON_COPY = (
    '<span class="material-symbols-outlined i-copy">content_copy</span>'
    '<span class="material-symbols-outlined i-check">check</span>'
)


def inlined_css() -> str:
    blocks = []
    for name in CSS_FILES:
        text = (CSS_DIR / name).read_text(encoding="utf-8")
        text = text.replace(
            'url("vendor/fonts/', f'url("{FONT_DIR.as_uri()}/'
        )
        blocks.append(f"<style>\n{text}\n</style>")
    return "\n".join(blocks)


def bubble(intro: str, extra_class: str = "", frame_class: str = "") -> str:
    cls = "bubble model markdown" + (f" {extra_class}" if extra_class else "")
    frame = (
        '<div class="inline-image-frame'
        + (f" {frame_class}" if frame_class else "")
        + '">'
    )
    return (
        f'<div class="{cls}">'
        f"<p>{intro}</p>"
        f"{frame}"
        '<img src="sample.svg" alt="sample chart" width="420">'
        f'<button type="button" class="copy-btn glass image-download">{ICON_DOWNLOAD}</button>'
        "</div>"
        f'<button type="button" class="copy-btn glass">{ICON_COPY}</button>'
        "</div>"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "sample.svg").write_text(SVG_IMAGE, encoding="utf-8")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Inline image download button: before vs after</title>
{inlined_css()}
<style>
  body {{ padding: 32px; }}
  #chat {{ max-width: 560px; margin: 0 auto; }}
  .force .copy-btn {{ opacity: 1 !important; pointer-events: auto !important; }}
  .inline-image-frame.before .copy-btn {{ top: 7px; right: 7px; }}
  .bubble.model .inline-image-frame.before img {{ margin: 6px 0; }}
  .label {{
    font-family: sans-serif; font-size: 13px; font-weight: 700;
    color: #666; margin: 18px 0 6px;
  }}
</style>
</head>
<body>
<div id="chat">
  <div class="label">BEFORE - 7px inset (the complaint)</div>
  {bubble("Button forced visible; inset reverted to 7px.", "force", "before")}
  <div class="label">AFTER - 12px inset (the fix)</div>
  {bubble("Button forced visible; current CSS, 12px.", "force")}
  <div class="label">LIVE - current CSS, hover to reveal</div>
  {bubble("Real behavior after the fix: hover this bubble.")}
</div>
<script>
  document.querySelectorAll("#chat > .bubble").forEach((bubble, i) => {{
    const btn = bubble.querySelector(".image-download");
    const cs = getComputedStyle(btn);
    const badge = document.createElement("div");
    badge.style.cssText =
      "font-family:monospace;font-size:12px;color:#333;margin-top:6px";
    badge.textContent =
      "computed inset: top " + cs.top + ", right " + cs.right;
    bubble.appendChild(badge);
  }});
</script>
</body>
</html>
"""
    out_path = OUT_DIR / "inline_image_preview.html"
    out_path.write_text(html, encoding="utf-8")
    print(out_path)


if __name__ == "__main__":
    main()
