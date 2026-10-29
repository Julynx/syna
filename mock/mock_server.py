"""Local test rig for the Syna narration pipeline.

Serves mock/narration_lab.html on http://127.0.0.1:8123/ together with
the real webui sources (so the page loads the production SpeechChunker)
and answers POST /api/speech/synthesize in two modes:

- "mock": generates a tone WAV whose duration grows with the text, with
  a small artificial latency — no external calls. Consecutive chunks get
  rising tones, so the playback order is audible.
- "server": proxies to a running Syna server (default
  http://127.0.0.1:8000/api/speech/synthesize) with real Gemini TTS.
  The per-process token is read automatically from the server's own
  page (window.SYNA_CONFIG); a token pasted in the page overrides it.

Run: uv run python mock/mock_server.py [--port 8123] [--target URL]
"""

import argparse
import io
import json
import math
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = "http://127.0.0.1:8000/api/speech/synthesize"
TONE_HZ = [329.63, 392.0, 440.0, 523.25]
SAMPLE_RATE_HZ = 11025
MOCK_LATENCY_S = 1.2

STATIC_ROOTS = ("/webui/", "/mock/")
STATIC_EXTENSIONS = {".js": "text/javascript", ".html": "text/html", ".css": "text/css"}
SYNA_CONFIG_PATTERN = re.compile(
    r"window\.SYNA_CONFIG = (\{.*?\});</script>", re.DOTALL
)
token_cache = {}


def tone_wav(duration_s: float, tone_index: int) -> bytes:
    """Render a sine tone as a 16-bit mono WAV, for mock synthesis."""
    frames = bytearray()
    for i in range(int(duration_s * SAMPLE_RATE_HZ)):
        envelope = min(1.0, i / (0.05 * SAMPLE_RATE_HZ))
        sample = math.sin(
            2 * math.pi * TONE_HZ[tone_index % len(TONE_HZ)] * i / SAMPLE_RATE_HZ
        )
        frames += struct.pack("<h", int(12000 * envelope * sample))
    container = io.BytesIO()
    with wave.open(container, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE_HZ)
        wav_file.writeframes(bytes(frames))
    return container.getvalue()


class Handler(BaseHTTPRequestHandler):
    """Routes the lab page, the webui sources, and the synthesis API."""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        """Serve the lab page and the static webui/mock sources."""
        path = self.path.split("?")[0]
        if path == "/":
            path = "/mock/narration_lab.html"
        if not path.startswith(STATIC_ROOTS):
            return self._json(404, {"detail": "not found"})
        file_path = (ROOT / path.lstrip("/")).resolve()
        if ROOT not in file_path.parents or file_path.suffix not in STATIC_EXTENSIONS:
            return self._json(404, {"detail": "not found"})
        if not file_path.is_file():
            return self._json(404, {"detail": "not found"})
        self._send(200, file_path.read_bytes(), STATIC_EXTENSIONS[file_path.suffix])

    def do_POST(self) -> None:
        """Synthesize one passage through the mock generator or the real server."""
        if self.path != "/api/speech/synthesize":
            return self._json(404, {"detail": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            request = json.loads(self.rfile.read(length))
            text = str(request.get("text", ""))
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"detail": "invalid request body"})

        if request.get("mode") == "server":
            return self._proxy(text, request)
        duration = min(8.0, max(1.0, len(text) / 1000 * 2.0))
        time.sleep(MOCK_LATENCY_S)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("X-Mock-Duration", f"{duration:.2f}")
        body = tone_wav(duration, self.server.chunk_counter())
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, text: str, request: dict) -> None:
        """Forward one synthesis request to the running Syna server."""
        target = str(request.get("target") or DEFAULT_TARGET)
        token = str(request.get("token") or "")
        if not token:
            token = auto_token(target)
            if token is None:
                return self._json(
                    502,
                    {
                        "detail": "the Syna server did not return its page; is it "
                        "running at "
                        + urllib.parse.urlsplit(target).scheme
                        + "://"
                        + urllib.parse.urlsplit(target).netloc
                        + " ?"
                    },
                )
        headers = {
            "Content-Type": "application/json",
            "X-Syna-Token": token,
        }
        try:
            upstream = urllib.request.Request(
                target,
                data=json.dumps({"text": text}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(upstream, timeout=180) as response:
                self._send(
                    200,
                    response.read(),
                    response.headers.get("Content-Type", "audio/wav"),
                )
        except urllib.error.HTTPError as error:
            self._json(error.code, {"detail": f"Syna server replied: {error.reason}"})
        except urllib.error.URLError as error:
            self._json(502, {"detail": f"cannot reach the Syna server: {error.reason}"})


def auto_token(target: str) -> str | None:
    """Read the Syna server's per-process token from its own page.

    The server embeds window.SYNA_CONFIG in the HTML shell it serves, so
    the lab does not need the token pasted by hand; the value is cached
    per origin because the page fetch is heavier than a synthesis call.
    """
    parts = urllib.parse.urlsplit(target)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin in token_cache:
        return token_cache[origin]
    try:
        with urllib.request.urlopen(origin + "/", timeout=10) as response:
            html = response.read().decode("utf-8")
    except (urllib.error.URLError, UnicodeDecodeError):
        return None
    match = SYNA_CONFIG_PATTERN.search(html)
    if not match:
        return None
    try:
        token = json.loads(match.group(1)).get("auth_token")
    except json.JSONDecodeError:
        return None
    if not token:
        return None
    token_cache[origin] = token
    return token


class LabServer(ThreadingHTTPServer):
    """HTTP server that counts chunks so mock tones rise in pitch."""

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.counter = 0

    def chunk_counter(self) -> int:
        current = self.counter
        self.counter += 1
        return current


def main() -> None:
    """Start the lab server on localhost."""
    parser = argparse.ArgumentParser(description="Syna narration lab server")
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    server = LabServer(("127.0.0.1", args.port), Handler)
    print(f"Narration lab: http://127.0.0.1:{args.port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
