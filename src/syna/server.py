"""FastAPI web server exposing Syna agent sessions over HTTP and WebSocket."""

import asyncio
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from posixpath import basename, normpath
from typing import Any

import uvicorn
from dotenv import dotenv_values
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from . import interaction, setup, speech
from .config import (
    DEFAULT_IMAGE_GENERATION_ENABLED,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_MODEL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_VOICE,
    DEFAULT_VOICE_ENABLED,
    asset_path,
    get_data_home,
    get_logger,
    load_config,
    read_asset_text,
)
from .imaging import (
    MAX_SERVE_BYTES,
    RECOMPRESS_BYTES,
    fit_image_to_box,
    image_exceeds_box,
)
from .session import AgentSession, shared_file_metadata
from .state import (
    create_container,
    destroy_container,
    register_container,
    unregister_container,
)
from .tools import _reset_browser

CONTAINER_START_TIMEOUT_S = 120
EVENT_LOG_MAX_ITEMS = 500
JANITOR_INTERVAL_S = 15
NEW_CHAT_STOP_TIMEOUT_S = 20
IMAGE_CACHE_LIMIT = 32
SERVER_ENTRY_POINT = "syna-server"
EXECUTABLE_SUFFIXES = (".exe", ".bat", ".cmd")

SERVED_IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

config = load_config()
logger = get_logger()
speech_settings = speech.load_speech_settings()

_sessions: dict[str, "SessionRecord"] = {}
_registry_lock = threading.Lock()


class SessionRecord:
    """Owns one client's agent session, container, queues and shared files."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.inbox: queue.Queue = queue.Queue()
        self.outbound: queue.Queue = queue.Queue()
        self.event_log: list[dict] = []
        self.event_log_lock = threading.Lock()
        self.shared: dict[str, dict] = {}
        self.pending_question = interaction.PendingQuestion()
        self.image_cache: dict[str, Path] = {}
        self.tmp_dir = Path(tempfile.mkdtemp(prefix=f"syna-{client_id[:8]}-"))
        self.container = None
        self.container_ready = threading.Event()
        self.busy = threading.Event()
        self.connected = False
        self.shutdown = threading.Event()
        self.last_activity = time.time()
        self.session = AgentSession(
            on_event=self._on_session_event,
            emit_tool_errors=True,
            persist_outputs=True,
            session_id=client_id,
        )
        self.worker = threading.Thread(
            target=self._run_worker,
            name=f"syna-worker-{client_id[:8]}",
            daemon=True,
        )
        self.worker.start()

    def touch(self):
        self.last_activity = time.time()

    def push(self, event: dict, persistent: bool = True):
        """Queue an event for delivery over the WebSocket."""
        if persistent:
            with self.event_log_lock:
                self.event_log.append(event)
                if len(self.event_log) > EVENT_LOG_MAX_ITEMS:
                    self.event_log.pop(0)
        self.outbound.put(event)

    def wait_for_container(self, timeout: float = CONTAINER_START_TIMEOUT_S):
        """Block until this session's container is running."""
        if not self.container_ready.wait(timeout=timeout):
            raise TimeoutError("Container did not start in time.")

    def share_file(self, metadata: dict):
        """Copy an shared pod path to the host and register it for download."""
        source_path = metadata["path"]
        name = metadata["name"]
        is_dir = metadata["is_dir"]
        token = uuid.uuid4().hex

        copy_dir = self.tmp_dir / token
        if not self.tmp_dir.exists():
            logger.warning(
                "Temp dir for session %s was removed; recreating it",
                self.client_id,
            )
            self.tmp_dir.mkdir(parents=True)
        copy_dir.mkdir()
        self.container.copy_from(source_path, str(copy_dir))
        source_copy = copy_dir / name

        if is_dir:
            archive_base = self.tmp_dir / token
            archive_path = Path(
                shutil.make_archive(str(archive_base), "zip", str(source_copy))
            )
            name = f"{name}.zip"
            host_path = archive_path
        else:
            host_path = source_copy

        self.shared[token] = {
            "name": name,
            "path": str(host_path),
            "size": host_path.stat().st_size,
        }
        logger.info(
            "Shared '%s' as token %s for session %s", source_path, token, self.client_id
        )
        self.push(
            {
                "type": "file",
                "name": name,
                "size": self.shared[token]["size"],
                "url": f"/api/files/{self.client_id}/{token}",
            }
        )

    def answer_pending_question(self, answer: str) -> bool:
        """Deliver a client's answer to the pending ask_user question.

        Returns False when no question is pending, so a stale or
        duplicated answer is ignored instead of resolved twice.
        """
        return self.pending_question.resolve(answer)

    def release_pending_question(self) -> None:
        """Unblock a pending ask_user question with a cancellation sentinel.

        Called when the user stops the task, starts a new chat, or the
        session is torn down: the blocked tool unwinds as a failed call
        and the turn ends cleanly.
        """
        self.pending_question.resolve(None)

    def _answer_question(self, question_text: str, choices: list) -> str:
        """Push one ask_user question to the client and wait for the answer.

        Runs on the tool worker thread; returns the user's answer, or
        raises InteractionCancelled when the question is released.
        """
        answers = self.pending_question.open()
        try:
            answer = answers.get()
        finally:
            self.pending_question.resolve(None)
        if answer is None:
            raise interaction.InteractionCancelled(
                "The user stopped the task before answering."
            )
        return answer

    def teardown(self):
        """Signal the worker to stop; it releases the container and temp files."""
        self.release_pending_question()
        self.shutdown.set()

    def _on_session_event(self, event: dict):
        metadata = shared_file_metadata(event)
        if metadata is not None:
            try:
                self.share_file(metadata)
            except Exception as exc:
                logger.exception("Failed to share file for session %s", self.client_id)
                self.push({"type": "error", "text": f"Could not share file: {exc}"})
            event = {key: value for key, value in event.items() if key != "output"}
        self.push(event)

    def _run_worker(self):
        """Own this session's container and process the inbox sequentially."""
        try:
            self.container = create_container()
        except Exception as exc:
            logger.exception("Container creation failed for session %s", self.client_id)
            self.push({"type": "error", "text": f"Could not start container: {exc}"})
            return
        register_container(self.container)
        self.container_ready.set()
        logger.info("Container ready for session %s", self.client_id)

        try:
            while not self.shutdown.is_set():
                try:
                    item = self.inbox.get(timeout=1.0)
                except queue.Empty:
                    continue
                if item.get("kind") == "message":
                    self.session.clear_cancel()
                    self._run_turn(item["content"])
        finally:
            unregister_container()
            try:
                destroy_container(self.container)
            except Exception:
                logger.exception(
                    "Container cleanup failed for session %s", self.client_id
                )
            try:
                self.session.close()
            except Exception:
                logger.exception(
                    "Model client cleanup failed for session %s", self.client_id
                )
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
            logger.info("Session %s torn down", self.client_id)

    def _run_turn(self, user_text: str):
        self.busy.set()
        self.push({"type": "busy"}, persistent=False)
        interaction.register_responder(self._answer_question)
        try:
            self.session.run_turn(user_text)
        except Exception:
            logger.exception("Turn failed for session %s", self.client_id)
            self.push(
                {"type": "error", "text": "The task failed unexpectedly. Please retry."}
            )
        finally:
            self.release_pending_question()
            interaction.unregister_responder()
            self.busy.clear()
            self.push({"type": "ready"}, persistent=False)


def _sanitize_client_id(client_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", client_id)[:64]
    return cleaned or uuid.uuid4().hex


def get_or_create_session(client_id: str) -> SessionRecord:
    """Return the session for a client id, creating it on first use."""
    client_id = _sanitize_client_id(client_id)
    with _registry_lock:
        record = _sessions.get(client_id)
        if record is None:
            logger.info("Creating session for client %s", client_id)
            record = SessionRecord(client_id)
            _sessions[client_id] = record
        record.touch()
        return record


def shutdown_all_sessions():
    """Request teardown of every live session."""
    with _registry_lock:
        records = list(_sessions.values())
    for record in records:
        record.teardown()
    for record in records:
        record.worker.join(timeout=10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    janitor = asyncio.create_task(_janitor_loop())
    yield
    janitor.cancel()
    await asyncio.to_thread(shutdown_all_sessions)


async def _janitor_loop():
    """Tear down sessions left disconnected and idle past the timeout."""
    timeout_s = config.get("session_idle_timeout_min", 15) * 60
    while True:
        await asyncio.sleep(JANITOR_INTERVAL_S)
        now = time.time()
        with _registry_lock:
            stale = [
                client_id
                for client_id, record in _sessions.items()
                if not record.connected
                and not record.busy.is_set()
                and now - record.last_activity > timeout_s
            ]
            for client_id in stale:
                _sessions[client_id].teardown()
                _sessions.pop(client_id)
        for client_id in stale:
            logger.info("Reaped idle session %s", client_id)


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount(
    "/vendor",
    StaticFiles(directory=str(asset_path("vendor"))),
    name="vendor",
)

# Per-process token required on /api endpoints and the WebSocket. Set by
# main() for real server runs; left unset in embedded use and tests, which
# disables the checks below entirely.
AUTH_TOKEN: str | None = None
AUTH_HEADER = "X-Syna-Token"
AUTH_QUERY = "token"


def request_token(request: Request) -> str:
    """The client's token from the header or, for direct links, the query."""
    return request.headers.get(AUTH_HEADER) or request.query_params.get(AUTH_QUERY, "")


def _loopback_binding() -> bool:
    """Whether the server listens on a loopback interface.

    Host-header validation only makes sense then: bound to 0.0.0.0, the
    Host header legitimately varies with the LAN address used to connect.
    """
    host = str(config.get("server_host", "127.0.0.1"))
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


LOCAL_ORIGIN_PATTERN = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")
HOST_HEADER_PATTERN = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")


def _host_header_allowed(host_header: str) -> bool:
    """Whether a request's Host header names this server's own address.

    A DNS-rebound page's requests carry the attacker's domain as Host,
    so anything but the configured loopback binding is refused.
    """
    if not host_header or not _loopback_binding():
        return bool(host_header)
    return bool(HOST_HEADER_PATTERN.match(host_header.lower()))


def _is_local_origin(origin: str) -> bool:
    """Whether an Origin header comes from a local loopback interface."""
    if not origin:
        return True
    return bool(LOCAL_ORIGIN_PATTERN.match(origin.lower()))


@app.middleware("http")
async def enforce_local_origin(request: Request, call_next):
    """Reject requests that did not come from the trusted local UI.

    With a token in force: the Host header must name the server's own
    loopback address, and /api and /setup endpoints must present the
    token (header or query — query because <img> and download links
    cannot set headers). Static assets, the HTML shell, and the setup
    wizard page stay open so the page can bootstrap and learn the token.
    """
    if AUTH_TOKEN:
        if not _host_header_allowed(request.headers.get("host", "")):
            return JSONResponse(
                status_code=403, content={"detail": "Untrusted Host header."}
            )
        needs_token = request.url.path.startswith("/api/") or (
            request.url.path.startswith("/setup/") and len(request.url.path) > 7
        )
        if needs_token and request_token(request) != AUTH_TOKEN:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid session token."},
            )
    if request.url.path == "/" and setup.setup_incomplete():
        return RedirectResponse("/setup")
    return await call_next(request)


@app.get("/")
def index():
    """Serve the chat GUI with runtime settings injected."""
    html = read_asset_text("chat.html")
    gui_config = {
        "max_visible_tool_calls": config.get("max_visible_tool_calls", 3),
        "voice": speech.client_configuration(speech_settings, config),
        "auth_token": AUTH_TOKEN,
    }
    # "</" escaped so no value can close the script tag early; AUTH_TOKEN
    # is url-safe anyway, but the config shape is user-editable.
    payload = json.dumps(gui_config).replace("</", "<\\/")
    script = f"<script>window.SYNA_CONFIG = {payload};</script>"
    return HTMLResponse(html.replace("</head>", script + "</head>"))


@app.get("/setup")
def setup_page():
    """Serve the first-run setup wizard; redirect to chat when done."""
    if not setup.setup_incomplete():
        return RedirectResponse("/")
    html = read_asset_text("setup.html")
    gui_config = {
        "auth_token": AUTH_TOKEN,
        "issues_url": setup.ISSUES_URL,
        "initial_config": setup.config_status(),
        "providers": setup.PROVIDERS,
    }
    payload = json.dumps(gui_config).replace("</", "<\\/")
    script = f"<script>window.SYNA_CONFIG = {payload};</script>"
    return HTMLResponse(html.replace("</head>", script + "</head>"))


class SetupConfigRequest(BaseModel):
    """The provider settings the wizard collects."""

    provider: str
    base_url: str | None = None
    api_key: str = ""
    model: str
    stt_model: str | None = None
    tts_model: str | None = None
    tts_voice: str | None = None
    image_model: str | None = None
    thinking_effort: str | None = None


@app.post("/setup/api/config")
async def save_setup_config(request: SetupConfigRequest):
    """Validate and write the provider settings into the data home .env."""
    if request.provider == "custom":
        if not request.base_url or not request.base_url.startswith(
            ("http://", "https://")
        ):
            raise HTTPException(
                status_code=400,
                detail="A valid endpoint URL is required for a custom provider.",
            )
        base_url = request.base_url.rstrip("/")
    elif request.provider in setup.PROVIDERS:
        preset = setup.PROVIDERS[request.provider]
        base_url = preset["base_url"]
        if preset["needs_key"] and not request.api_key.strip():
            raise HTTPException(
                status_code=400,
                detail=f"An API key is required for {preset['name']}.",
            )
    else:
        raise HTTPException(status_code=400, detail="Unknown provider.")
    if not request.model.strip():
        raise HTTPException(status_code=400, detail="A model name is required.")

    env_updates = {
        "MODEL": request.model.strip(),
        "BASE_URL": base_url,
    }
    if request.api_key.strip():
        env_updates["OPENROUTER_API_KEY"] = request.api_key.strip()
    if request.stt_model:
        env_updates["STT_MODEL"] = request.stt_model.strip()
    if request.tts_model:
        env_updates["TTS_MODEL"] = request.tts_model.strip()
    if request.tts_voice:
        env_updates["TTS_VOICE"] = request.tts_voice.strip()
    if request.image_model:
        env_updates["IMAGE_MODEL"] = request.image_model.strip()
    if request.thinking_effort:
        env_updates["THINKING_EFFORT"] = request.thinking_effort.strip()

    env_path = setup.write_env_file(env_updates)
    logger.info("Setup wrote %s", env_path)
    return {"status": "ok", "path": str(env_path)}


@app.get("/setup/api/detect")
def setup_detect():
    """Dependency status for the wizard's dependency steps."""
    return setup.detect_all()


@app.post("/setup/api/install/{dependency}")
async def setup_install(dependency: str):
    """Try to install one dependency; report the outcome as data."""
    installers = {"podman": setup.install_podman, "docker": setup.install_docker}
    if dependency == "wsl":
        found, tail, reboot = setup.install_wsl()
        return {
            "status": "reboot_required" if reboot else ("ok" if found else "failed"),
            "log": tail,
        }
    if dependency not in installers:
        raise HTTPException(status_code=404, detail="Unknown dependency.")
    found, tail = installers[dependency]()
    return {"status": "ok" if found else "failed", "log": tail}


@app.post("/setup/api/sanity")
async def setup_sanity():
    """Run the closing sanity check: engine + one-token chat completion."""
    return setup.sanity_check()


@app.post("/api/upload/{client_id}")
async def upload_file(client_id: str, file: UploadFile, dest: str | None = None):
    """Store an uploaded file on the host and copy it into the pod."""
    record = get_or_create_session(client_id)
    safe_name = Path(file.filename or "upload.bin").name
    destination = dest or config.get("upload_dest", "/home/user/uploads")
    try:
        record.wait_for_container()
        with tempfile.TemporaryDirectory(prefix="syna-upload-") as temp_dir:
            local_path = Path(temp_dir, safe_name)
            await asyncio.to_thread(local_path.write_bytes, await file.read())
            await asyncio.to_thread(
                record.container.copy_to, str(local_path), destination
            )
    except Exception as exc:
        logger.exception("Upload failed for session %s", client_id)
        raise HTTPException(status_code=502, detail=f"Upload failed: {exc}") from exc
    record.push({"type": "file_sent", "name": safe_name, "dest": destination})
    pod_path = f"{destination.rstrip('/')}/{safe_name}"
    record.session.notify_file_upload(safe_name, pod_path)
    return {"status": "ok", "name": safe_name, "dest": destination}


@app.post("/api/new-chat/{client_id}")
async def new_chat(client_id: str):
    """Reset a session: stop any running turn, destroy the VM and drop the
    conversation so the next connection starts completely fresh.

    Returns before the client reconnects; the replacement session (with a
    new container) is created on the next WebSocket connect. If a turn is
    still stuck past the stop timeout, teardown proceeds anyway — the old
    worker finishes cleaning up in the background while the client gets a
    brand new session.
    """
    with _registry_lock:
        record = _sessions.get(client_id)
    if record is None:
        return {"status": "ok", "reset": False}
    logger.info("New chat requested for session %s", client_id)
    if record.busy.is_set():
        record.session.cancel()
        record.release_pending_question()
        deadline = time.time() + NEW_CHAT_STOP_TIMEOUT_S
        while record.busy.is_set() and time.time() < deadline:
            await asyncio.sleep(0.1)
    record.teardown()
    await asyncio.to_thread(record.worker.join, 10)
    with _registry_lock:
        _sessions.pop(client_id, None)
    await asyncio.to_thread(_reset_browser)
    return {"status": "ok", "reset": True}


@app.get("/api/files/{client_id}/{token}")
def download_file(client_id: str, token: str):
    """Stream a previously shared file to the client."""
    record = _sessions.get(client_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown session.")
    info = record.shared.get(token)
    if info is None:
        raise HTTPException(status_code=404, detail="Unknown file.")
    return FileResponse(info["path"], filename=info["name"])


@app.get("/api/image/{client_id}")
def serve_pod_image(client_id: str, path: str):
    """Stream an image from the session pod for inline rendering.

    The client rewrites markdown image paths (e.g. from Syna's replies)
    to this endpoint. Only image files under /workspace are eligible;
    everything is copied out of the pod, so host files are unreachable.
    """
    record = _sessions.get(client_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown session.")
    cleaned = normpath(path or "")
    extension = Path(cleaned).suffix.lower()
    if (
        not cleaned.startswith("/workspace/")
        or extension not in SERVED_IMAGE_EXTENSIONS
    ):
        raise HTTPException(
            status_code=400,
            detail="Only image files under /workspace can be displayed.",
        )
    return _prepare_pod_image(record, cleaned)


def _prepare_pod_image(record, pod_path: str) -> FileResponse:
    """Copy, normalize, and cache one pod image for serving.

    Sources larger than MAX_SERVE_BYTES are rejected outright. Sources
    that fit the 1024x1024 box and stay under RECOMPRESS_BYTES are served
    untouched (animated GIFs survive); everything else is downscaled into
    the box and converted to PNG. Each request gets its own copy, so a
    regenerated image at the same pod path is never served stale.
    """
    serve_dir = record.tmp_dir / "images"
    serve_dir.mkdir(exist_ok=True)
    token_dir = serve_dir / uuid.uuid4().hex
    token_dir.mkdir()
    try:
        record.container.copy_from(pod_path, str(token_dir))
        source = token_dir / basename(pod_path)
        if not source.is_file():
            raise FileNotFoundError(pod_path)
    except HTTPException:
        shutil.rmtree(token_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(token_dir, ignore_errors=True)
        message = str(exc).lower()
        if "404" in message or "no such file" in message:
            raise HTTPException(
                status_code=404, detail="Image not found in the pod."
            ) from exc
        logger.exception("Image copy failed for session %s", record.client_id)
        raise HTTPException(
            status_code=502, detail=f"Could not copy the image from the pod: {exc}"
        ) from exc

    try:
        size = source.stat().st_size
        if size > MAX_SERVE_BYTES:
            raise HTTPException(
                status_code=413, detail="The image is too large to display."
            )
        if size <= RECOMPRESS_BYTES:
            with Image.open(source) as img:
                if not image_exceeds_box(img):
                    return _serve_image_copy(
                        record, token_dir, source, _media_type_for(pod_path)
                    )
    except HTTPException:
        shutil.rmtree(token_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(token_dir, ignore_errors=True)
        raise HTTPException(
            status_code=415, detail="Could not read the file as an image."
        ) from exc

    target = token_dir / "served.png"
    try:
        fit_image_to_box(source, target)
    except Exception as exc:
        shutil.rmtree(token_dir, ignore_errors=True)
        raise HTTPException(
            status_code=415, detail="Could not read the file as an image."
        ) from exc
    return _serve_image_copy(record, token_dir, target, "image/png")


def _media_type_for(pod_path: str) -> str:
    """Map a validated pod image path to its response media type."""
    return SERVED_IMAGE_EXTENSIONS[Path(pod_path).suffix.lower()]


def _serve_image_copy(
    record, token_dir: Path, file_path: Path, media_type: str
) -> FileResponse:
    """Track a prepared copy for eviction and build its file response.

    The LRU cap bounds the temp footprint across history replays; the
    session tmp dir wipe on teardown removes whatever remains.
    """
    record.image_cache[uuid.uuid4().hex] = token_dir
    while len(record.image_cache) > IMAGE_CACHE_LIMIT:
        oldest_key = next(iter(record.image_cache))
        oldest_dir = record.image_cache.pop(oldest_key)
        shutil.rmtree(oldest_dir, ignore_errors=True)
    return FileResponse(
        file_path,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


class SynthesisRequest(BaseModel):
    """Body of a speech synthesis request."""

    text: str


class SettingsUpdateRequest(BaseModel):
    """Payload for updating settings from the GUI."""

    env: dict[str, str]
    config: dict[str, Any]


@app.get("/api/settings")
def get_settings():
    """Return all current settings, recommended defaults, and provider presets."""
    env = dotenv_values(get_data_home() / ".env")
    runtime_cfg = load_config()
    defaults = {
        "provider": "openrouter",
        "api_key": "",
        "base_url": "https://openrouter.ai/api/v1",
        "model": DEFAULT_MODEL,
        "thinking_effort": "medium",
        "verifier_model": "",
        "verifier_model_effort": "high",
        "stt_model": DEFAULT_STT_MODEL,
        "tts_model": DEFAULT_TTS_MODEL,
        "tts_voice": DEFAULT_TTS_VOICE,
        "speech_language": "en",
        "image_model": DEFAULT_IMAGE_MODEL,
        "max_steps_per_turn": 100,
        "command_timeout_s": 50,
        "tool_call_timeout_s": 60,
        "pod_memory_mb": 8192,
        "context_budget_tokens": 100000,
        "max_command_output_chars": 40000,
        "voice_enabled": DEFAULT_VOICE_ENABLED,
        "image_generation_enabled": DEFAULT_IMAGE_GENERATION_ENABLED,
    }
    current_base = (env.get("BASE_URL") or "").strip()
    detected_provider = "openrouter"
    if current_base:
        for p_key, p_val in setup.PROVIDERS.items():
            if p_val["base_url"].rstrip("/") == current_base.rstrip("/"):
                detected_provider = p_key
                break
        else:
            detected_provider = "custom"

    return {
        "env": {
            "provider": detected_provider,
            "api_key": (env.get("OPENROUTER_API_KEY") or "").strip(),
            "base_url": current_base,
            "model": (env.get("MODEL") or "").strip(),
            "thinking_effort": (env.get("THINKING_EFFORT") or "").strip()
            or defaults["thinking_effort"],
            "verifier_model": (env.get("VERIFIER_MODEL") or "").strip(),
            "verifier_model_effort": (env.get("VERIFIER_MODEL_EFFORT") or "").strip()
            or defaults["verifier_model_effort"],
            "stt_model": (env.get("STT_MODEL") or "").strip(),
            "tts_model": (env.get("TTS_MODEL") or "").strip(),
            "tts_voice": (env.get("TTS_VOICE") or "").strip() or defaults["tts_voice"],
            "speech_language": (env.get("SPEECH_LANGUAGE") or "").strip()
            or defaults["speech_language"],
            "image_model": (env.get("IMAGE_MODEL") or "").strip(),
        },
        "config": {
            "max_steps_per_turn": runtime_cfg.get(
                "max_steps_per_turn", defaults["max_steps_per_turn"]
            ),
            "command_timeout_s": runtime_cfg.get(
                "command_timeout_s", defaults["command_timeout_s"]
            ),
            "tool_call_timeout_s": runtime_cfg.get(
                "tool_call_timeout_s", defaults["tool_call_timeout_s"]
            ),
            "pod_memory_mb": runtime_cfg.get(
                "pod_memory_mb", defaults["pod_memory_mb"]
            ),
            "context_budget_tokens": runtime_cfg.get(
                "context_budget_tokens", defaults["context_budget_tokens"]
            ),
            "max_command_output_chars": runtime_cfg.get(
                "max_command_output_chars", defaults["max_command_output_chars"]
            ),
            "voice_enabled": runtime_cfg.get(
                "voice_enabled", defaults["voice_enabled"]
            ),
            "image_generation_enabled": runtime_cfg.get(
                "image_generation_enabled",
                defaults["image_generation_enabled"],
            ),
        },
        "defaults": defaults,
        "providers": setup.PROVIDERS,
    }


@app.post("/api/settings")
async def save_settings(request: SettingsUpdateRequest):
    """Save updated .env and config.json settings and schedule a server restart."""
    from .config import save_config

    env_data = request.env
    env_updates = {}
    if "api_key" in env_data:
        env_updates["OPENROUTER_API_KEY"] = env_data["api_key"].strip()
    if "base_url" in env_data:
        env_updates["BASE_URL"] = env_data["base_url"].strip()
    if "model" in env_data:
        env_updates["MODEL"] = env_data["model"].strip()
    if "thinking_effort" in env_data:
        env_updates["THINKING_EFFORT"] = env_data["thinking_effort"].strip()
    if "verifier_model" in env_data:
        env_updates["VERIFIER_MODEL"] = env_data["verifier_model"].strip()
    if "verifier_model_effort" in env_data:
        env_updates["VERIFIER_MODEL_EFFORT"] = env_data["verifier_model_effort"].strip()
    if "stt_model" in env_data:
        env_updates["STT_MODEL"] = env_data["stt_model"].strip()
    if "tts_model" in env_data:
        env_updates["TTS_MODEL"] = env_data["tts_model"].strip()
    if "tts_voice" in env_data:
        env_updates["TTS_VOICE"] = env_data["tts_voice"].strip()
    if "speech_language" in env_data:
        env_updates["SPEECH_LANGUAGE"] = env_data["speech_language"].strip()
    if "image_model" in env_data:
        env_updates["IMAGE_MODEL"] = env_data["image_model"].strip()

    setup.write_env_file(env_updates)

    if request.config:
        save_config(request.config)

    # Invalidate caches and reset any open browser
    load_config.cache_clear()
    global speech_settings, config
    config = load_config()
    speech_settings = speech.load_speech_settings()
    _reset_browser()
    _spawn_restart()
    return {"status": "ok", "restarting": True}


def _restart_argv() -> list[str]:
    """Build the argv that re-executes the server after a settings save.

    Preference order: the current entry point (an entry-point executable
    is re-executed directly, a script through the current interpreter),
    the globally installed syna-server (any PATH entry outside the active
    virtualenv), `uv run syna-server` for checkouts without a resolvable
    entry point, and finally the current interpreter with the original
    arguments.
    """
    argv0 = Path(sys.argv[0])
    candidate = argv0 if argv0.is_file() else argv0.with_suffix(".exe")
    if candidate.is_file():
        if candidate.suffix.lower() in EXECUTABLE_SUFFIXES:
            return [str(candidate), *sys.argv[1:]]
        return [sys.executable, str(candidate), *sys.argv[1:]]
    venv_root = Path(sys.prefix).resolve()
    found = shutil.which(SERVER_ENTRY_POINT)
    if found and not Path(found).resolve().is_relative_to(venv_root):
        return [found, *sys.argv[1:]]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", SERVER_ENTRY_POINT, *sys.argv[1:]]
    return [sys.executable, str(argv0), *sys.argv[1:]]


def _delayed_restart():
    time.sleep(0.6)
    try:
        shutdown_all_sessions()
    except Exception:
        pass
    argv = _restart_argv()
    get_logger().info("Restarting server with: %s", argv)
    print(f"Restarting server with: {' '.join(argv)}", file=sys.stderr, flush=True)
    try:
        os.execv(argv[0], argv)
    except OSError as exc:
        get_logger().exception("Failed to restart the server with %s", argv)
        print(
            f"Failed to restart the server with {' '.join(argv)}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        raise


def _spawn_restart() -> None:
    threading.Thread(target=_delayed_restart, daemon=True).start()


@app.post("/api/speech/transcribe")
async def transcribe_speech(file: UploadFile):
    """Transcribe a recorded voice clip into text."""
    audio_format = speech.audio_format_for(file.content_type)
    if audio_format is None:
        raise HTTPException(
            status_code=415, detail=f"Unsupported audio type: {file.content_type}"
        )
    audio_bytes = await file.read()
    try:
        text = await asyncio.to_thread(
            speech.transcribe_audio,
            audio_bytes,
            audio_format,
            speech_settings,
            speech_settings.language,
        )
    except speech.SpeechError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"text": text}


@app.post("/api/speech/synthesize")
async def synthesize_speech(request: SynthesisRequest):
    """Synthesize spoken audio for one passage of text."""
    text = request.text.strip()
    if not text:
        raise HTTPException(
            status_code=400, detail="Nothing to synthesize: the text is empty."
        )
    try:
        audio = await asyncio.to_thread(speech.synthesize_speech, text, speech_settings)
    except speech.SpeechError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=audio.content, media_type=audio.media_type)


async def _pump_events(record: SessionRecord, websocket: WebSocket):
    """Forward queued session events to the connected WebSocket client."""
    while True:
        try:
            event = await asyncio.to_thread(record.outbound.get, True, 1.0)
        except queue.Empty:
            continue
        try:
            await websocket.send_json(event)
        except Exception:
            return


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """Two-way realtime channel between one client and its session."""
    if AUTH_TOKEN:
        # A page's WebSocket is not same-origin restricted by browsers;
        # the server enforces it instead. A missing Origin comes from the
        # file:// fallback client and still needs the token; closing
        # before accepting denies the handshake with an HTTP 403.
        origin = websocket.headers.get("origin", "")
        if origin and not _is_local_origin(origin):
            await websocket.close(code=1008)
            return
        if websocket.query_params.get(AUTH_QUERY, "") != AUTH_TOKEN:
            await websocket.close(code=1008)
            return
    await websocket.accept()
    client_id = _sanitize_client_id(client_id)
    try:
        record = get_or_create_session(client_id)
    except Exception as exc:
        logger.exception("Session creation failed for client %s", client_id)
        await websocket.send_json(
            {"type": "error", "text": f"Could not start a session: {exc}"}
        )
        await websocket.close()
        return
    record.connected = True
    record.touch()

    with record.event_log_lock:
        history = list(record.event_log)
    try:
        for event in history:
            await websocket.send_json(event)
        await websocket.send_json({"type": "history_done"})
    except Exception:
        record.connected = False
        return

    pump_task = asyncio.create_task(_pump_events(record, websocket))
    try:
        while True:
            data = await websocket.receive_json()
            record.touch()
            message_type = data.get("type")
            if message_type == "message":
                content = str(data.get("content", "")).strip()
                if not content:
                    continue
                # A pending ask_user question is waiting: this message
                # is the typed answer, not the start of a new turn.
                if record.pending_question.active and record.answer_pending_question(
                    content
                ):
                    record.push({"type": "user_message", "content": content})
                    continue
                record.push(
                    {
                        "type": "user_message",
                        "content": content,
                        "queued": record.busy.is_set(),
                    }
                )
                record.inbox.put({"kind": "message", "content": content})
            elif message_type == "answer":
                choice = str(data.get("choice", "")).strip()
                if not choice:
                    continue
                if record.answer_pending_question(choice):
                    record.push({"type": "user_message", "content": choice})
            elif message_type == "stop":
                record.session.cancel()
                record.release_pending_question()
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        record.connected = False
        record.touch()


def main():
    """Start the Syna web server."""
    global AUTH_TOKEN
    AUTH_TOKEN = secrets.token_urlsafe(32)
    port = int(config.get("server_port", 8000))
    host = str(config.get("server_host", "127.0.0.1"))
    url = f"http://127.0.0.1:{port}/"

    if setup.setup_incomplete():
        import webbrowser

        def open_browser():
            time.sleep(1.5)
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()

    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="info",
        )
    except KeyboardInterrupt:
        pass
    finally:
        try:
            logger.info("Shutting down Syna server, cleaning up...")
            shutdown_all_sessions()
        except Exception:
            logger.exception("Error during server shutdown")
