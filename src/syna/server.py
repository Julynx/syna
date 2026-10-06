"""FastAPI web server exposing Syna agent sessions over HTTP and WebSocket."""

import asyncio
import json
import queue
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import get_logger, get_project_root, load_config
from .session import AgentSession
from .state import create_container, destroy_container, register_container, unregister_container

CONTAINER_START_TIMEOUT_S = 120
EVENT_LOG_MAX_ITEMS = 500
JANITOR_INTERVAL_S = 15
REPLAYED_EVENT_TYPES = {
    "user_message",
    "model_message",
    "tool_call",
    "file",
    "file_sent",
    "compaction",
    "error",
}

config = load_config()
logger = get_logger()

_sessions: dict[str, "SessionRecord"] = {}
_registry_lock = threading.Lock()


class SessionRecord:
    """Owns one client's agent session, container, queues and exposed files."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.inbox: queue.Queue = queue.Queue()
        self.outbound: queue.Queue = queue.Queue()
        self.event_log: list[dict] = []
        self.event_log_lock = threading.Lock()
        self.exposed: dict[str, dict] = {}
        self.tmp_dir = Path(tempfile.mkdtemp(prefix=f"syna-{client_id[:8]}-"))
        self.container = None
        self.container_ready = threading.Event()
        self.busy = threading.Event()
        self.connected = False
        self.shutdown = threading.Event()
        self.last_activity = time.time()
        self.session = AgentSession(
            on_event=self._on_session_event, emit_tool_errors=True
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

    def expose_file(self, metadata: dict):
        """Copy an exposed pod path to the host and register it for download."""
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

        self.exposed[token] = {
            "name": name,
            "path": str(host_path),
            "size": host_path.stat().st_size,
        }
        logger.info("Exposed '%s' as token %s for session %s",
                    source_path, token, self.client_id)
        self.push(
            {
                "type": "file",
                "name": name,
                "size": self.exposed[token]["size"],
                "url": f"/api/files/{self.client_id}/{token}",
            }
        )

    def teardown(self):
        """Signal the worker to stop; it releases the container and temp files."""
        self.shutdown.set()

    def _on_session_event(self, event: dict):
        if (
            event.get("type") == "tool_call"
            and event.get("status") == "finished"
            and event.get("name") == "expose_file"
            and isinstance(event.get("output"), dict)
            and "path" in event["output"]
        ):
            try:
                self.expose_file(event["output"])
            except Exception as exc:
                logger.exception("Failed to expose file for session %s", self.client_id)
                self.push({"type": "error", "text": f"Could not expose file: {exc}"})
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
                logger.exception("Model client cleanup failed for session %s", self.client_id)
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
            logger.info("Session %s torn down", self.client_id)

    def _run_turn(self, user_text: str):
        self.busy.set()
        self.push({"type": "busy"}, persistent=False)
        try:
            self.session.run_turn(user_text)
        except Exception:
            logger.exception("Turn failed for session %s", self.client_id)
            self.push(
                {"type": "error", "text": "The task failed unexpectedly. Please retry."}
            )
        finally:
            self.busy.clear()
            self.push({"type": "ready"}, persistent=False)


def _sanitize_client_id(client_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", client_id)[:64]
    return cleaned or uuid.uuid4().hex


def get_or_create_session(client_id: str) -> SessionRecord:
    """Return the session for a client id, creating it on first use."""
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
app.mount(
    "/vendor",
    StaticFiles(directory=get_project_root() / "assets" / "vendor"),
    name="vendor",
)


@app.get("/")
def index():
    """Serve the chat GUI with runtime settings injected."""
    chat_path = get_project_root() / "assets" / "chat.html"
    html = chat_path.read_text(encoding="utf-8")
    gui_config = {"max_visible_tool_calls": config.get("max_visible_tool_calls", 3)}
    script = f"<script>window.SYNA_CONFIG = {json.dumps(gui_config)};</script>"
    return HTMLResponse(html.replace("</head>", script + "</head>"))


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
            local_path.write_bytes(await file.read())
            await asyncio.to_thread(record.container.copy_to, str(local_path), destination)
    except Exception as exc:
        logger.exception("Upload failed for session %s", client_id)
        raise HTTPException(status_code=502, detail=f"Upload failed: {exc}") from exc
    record.push({"type": "file_sent", "name": safe_name, "dest": destination})
    pod_path = f"{destination.rstrip('/')}/{safe_name}"
    record.session.notify_file_upload(safe_name, pod_path)
    return {"status": "ok", "name": safe_name, "dest": destination}


@app.get("/api/files/{client_id}/{token}")
def download_file(client_id: str, token: str):
    """Stream a previously exposed file to the client."""
    record = _sessions.get(client_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown session.")
    info = record.exposed.get(token)
    if info is None:
        raise HTTPException(status_code=404, detail="Unknown file.")
    return FileResponse(info["path"], filename=info["name"])


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
                record.push(
                    {
                        "type": "user_message",
                        "content": content,
                        "queued": record.busy.is_set(),
                    }
                )
                record.inbox.put({"kind": "message", "content": content})
            elif message_type == "stop":
                record.session.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        record.connected = False
        record.touch()


def main():
    """Start the Syna web server."""
    try:
        uvicorn.run(
            app,
            host=config.get("server_host", "127.0.0.1"),
            port=config.get("server_port", 8000),
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
