"""FastAPI web server exposing Syna agent sessions over HTTP and WebSocket."""

import asyncio
import ipaddress
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from posixpath import basename, normpath
from typing import Any
from urllib.parse import quote

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

from . import interaction, setup
from .capabilities import processes, speech, subagents
from .pod_io import pod_copy_from
from .protocol.channel import harness_notice, unwrap_channel, wrap_channel
from .config import (
    DEFAULT_IMAGE_GENERATION_ENABLED,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_MODEL,
    DEFAULT_CONTINUOUS_CONVERSATION,
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
from .capabilities.imaging import (
    MAX_SERVE_BYTES,
    RECOMPRESS_BYTES,
    fit_image_to_box,
    image_exceeds_box,
)
from .conversation.session import AgentSession, shared_file_metadata
from .pod_init import shared_copy_notice
from .state import (
    create_container,
    destroy_container,
    register_container,
    unregister_container,
)
from .capabilities.tools import _reset_browser

CONTAINER_START_TIMEOUT_S = 120
EVENT_LOG_MAX_ITEMS = 500
JANITOR_INTERVAL_S = 15
CONTAINER_STARTING_TEXT = "Starting the container engine, please wait..."
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
    """Owns one client's agent session, container, queues and shared files.

    The inbox is a deque under a condition instead of a queue.Queue:
    "Send now" removes a specific queued item, which a Queue cannot do
    safely. One delivery queue per connected WebSocket: every open
    surface (a reloaded tab, a second window, the desktop app) receives
    every event, instead of a shared queue splitting them between
    connections."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.inbox: deque = deque()
        self.inbox_ready = threading.Condition()
        self.event_log: list[dict] = []
        self.event_log_lock = threading.Lock()
        self.subscribers: list[queue.Queue] = []
        self.shared: dict[str, dict] = {}
        self.pending_question = interaction.PendingQuestion()
        self.image_cache: dict[str, Path] = {}
        self.tmp_dir = Path(tempfile.mkdtemp(prefix=f"syna-{client_id[:8]}-"))
        self.container = None
        self.container_ready = threading.Event()
        self.container_starting = False
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
        """Log an event and deliver it to every connected WebSocket."""
        with self.event_log_lock:
            if persistent:
                self.event_log.append(event)
                if len(self.event_log) > EVENT_LOG_MAX_ITEMS:
                    self.event_log.pop(0)
            for subscriber in self.subscribers:
                subscriber.put(event)

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
        pod_copy_from(self.container, source_path, str(copy_dir))
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
        self.stop_background_work()

    def stop_background_work(self):
        """Stop this session's subagents and forget the registry.

        Called on teardown (new chat, janitor reaping, server
        shutdown): the pod is about to be destroyed, so its processes
        die with it, and the subagent runners only need to stop
        cleanly and record why. Registry entries are per-session; the
        drop keeps the host-side table from leaking them.
        """
        if self.container is None:
            return
        registry = processes.registry_for(self.container)
        with registry.lock:
            records = list(registry.subagents.values())
        for record in records:
            if record.state != "running":
                continue
            record.stopped_by = "teardown"
            record.kill_requested.set()
            if record.session is not None:
                record.session.cancel()
        processes.drop_registry(self.container)

    def _on_subagent_event(self, subagent_id: str, event: dict):
        """Stream one subagent event to connected clients.

        Non-persistent: the subagent viewer fetches the recorded
        history from the registry on open, so replaying every stream
        event into the reconnect history would flood it and evict the
        main conversation.
        """
        self.push(
            {"type": "subagent_message", "subagent_id": subagent_id, **event},
            persistent=False,
        )

    def _on_background_completion(self, notice: dict):
        """Announce finished background work and wake the worker.

        The done event is persistent (a compact row the client keeps)
        and always fires: the panel is a chat-level view of every
        spawn. The model-facing notice goes only to the spawner: a
        nested subagent receives it as an injection into its live
        turn, and a top-level spawn's notice travels through the same
        inbox user messages take, waking an idle worker or queueing
        behind an active turn as a harness notice. A notice whose
        parent is settled or gone is dropped, never re-routed to the
        top level. When a report already returned the final outcome,
        the notice is suppressed instead: work that ends on its own
        must reach the model exactly once. The queued item keeps the
        background identity so the worker can drop it at dequeue time
        if the outcome is collected in between.
        """
        self.push(
            {
                "type": "background_done",
                "id": notice.get("id"),
                "kind": notice.get("kind"),
                "summary": notice.get("summary"),
            }
        )
        model_notice = notice.get("model_notice")
        if not model_notice:
            return
        kind = notice.get("kind")
        background_id = notice.get("id")
        parent_id = notice.get("parent_id")
        if kind and background_id and self._wait_for_collected(kind, background_id):
            return
        if kind and background_id and parent_id:
            self._deliver_to_parent(parent_id, kind, background_id, model_notice)
            return
        self.enqueue_message(
            {
                "kind": "message",
                "id": uuid.uuid4().hex,
                "bg_kind": kind,
                "bg_id": background_id,
                "content": wrap_channel("HARNESS_NOTICE", harness_notice(model_notice)),
            }
        )

    def _deliver_to_parent(
        self, parent_id: str, kind: str, background_id: str, model_notice: str
    ) -> bool:
        """Inject a completion notice into the spawning subagent's turn.

        Returns whether it was delivered. Only a live, unsettled
        subagent parent takes the notice: the registry lookup covers
        unknown parents, `subagent_is_settled` covers killed and
        cap-stopped ones, and a missing session means the turn is
        already over. A dropped notice is logged, never re-routed to
        the top level — the panel still lists the completion, and the
        artifacts stay in the pod. Parentless work (spawned by the
        top-level agent) never enters here; the caller enqueues its
        notice directly.
        """
        registry = (
            processes.peek_registry(self.container)
            if self.container is not None
            else None
        )
        entry = registry.lookup(parent_id) if registry is not None else None
        if entry is None:
            logger.info(
                "Dropping the completion notice for '%s': its parent"
                " '%s' is not registered.",
                background_id,
                parent_id,
            )
            return False
        parent_kind, parent = entry
        session = parent.session
        if (
            parent_kind != "subagent"
            or subagents.subagent_is_settled(parent)
            or session is None
        ):
            logger.info(
                "Dropping the completion notice for '%s': its parent"
                " '%s' is no longer running.",
                background_id,
                parent_id,
            )
            return False
        session.queue_injection(
            {
                "kind": "message",
                "id": uuid.uuid4().hex,
                "bg_kind": kind,
                "bg_id": background_id,
                "notice_text": model_notice,
                "content": wrap_channel("HARNESS_NOTICE", harness_notice(model_notice)),
            }
        )
        return True

    def _wait_for_collected(self, kind: str, background_id: str) -> bool:
        """Whether the model already collected this record's outcome.

        wait_for, check, and kill all report a finished record's final
        outcome and set its collected flag when they do.
        """
        if self.container is None:
            return False
        registry = processes.peek_registry(self.container)
        if registry is None:
            return False
        entry = registry.lookup(background_id)
        return entry is not None and entry[1].outcome_collected

    def _notice_is_stale(self, item: dict) -> bool:
        """Whether a queued background notice lost the race to a report.

        wait_for, check, and kill set the record's collected flag when
        they return the final outcome; a notice enqueued before that
        must not wake the model with the same outcome a second time.
        """
        background_id = item.get("bg_id")
        if not background_id:
            return False
        return self._wait_for_collected(item.get("bg_kind") or "", background_id)

    def _merge_notice_batch(self, first: dict) -> dict:
        """Join every pending background notice into one wake message.

        Completion notices queue while a turn runs, and draining them
        one per turn burns a model round trip each. Notice items still
        at the head of the inbox ride along with the first one (a
        non-notice message stops the sweep and keeps its place), minus
        the ones whose outcome the model already collected. The bodies
        merge under a single channel wrapper, the same shape batched
        tool results use.

        Anything that is not itself a background notice — a queued user
        message above all — passes through untouched: its content is
        not channel-framed and must never be rewritten here.
        """
        if not first.get("bg_id"):
            return first
        items = [first]
        with self.inbox_ready:
            while self.inbox and self.inbox[0].get("bg_id"):
                items.append(self.inbox.popleft())
        bodies = []
        for entry in items:
            if self._notice_is_stale(entry):
                logger.info(
                    "Dropping the completion notice for '%s': the model"
                    " already collected its outcome.",
                    entry.get("bg_id"),
                )
                continue
            unwrapped = unwrap_channel(entry.get("content") or "")
            bodies.append(unwrapped[1] if unwrapped else entry.get("content", ""))
        if not bodies:
            return None
        first["content"] = wrap_channel("HARNESS_NOTICE", "\n\n".join(bodies))
        return first

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

    def enqueue_message(self, item: dict):
        """Add one message to the session's inbox and wake the worker."""
        with self.inbox_ready:
            self.inbox.append(item)
            self.inbox_ready.notify()

    def take_message(self, item_id: str) -> dict | None:
        """Remove and return the queued item with this id, if present."""
        with self.inbox_ready:
            for index, item in enumerate(self.inbox):
                if item.get("id") == item_id:
                    del self.inbox[index]
                    return item
        return None

    def _requeue_front(self, item: dict):
        """Put one item back at the head of the inbox (engine retry)."""
        with self.inbox_ready:
            self.inbox.appendleft(item)

    def _next_message(self, timeout: float) -> dict | None:
        """Wait for an inbox item, returning None on timeout."""
        with self.inbox_ready:
            if not self.inbox:
                self.inbox_ready.wait(timeout=timeout)
            return self.inbox.popleft() if self.inbox else None

    def _requeue_injections(self):
        """Return undelivered "Send now" injections to the inbox.

        A turn that ends before an opening arrives (final response,
        error, user stop) would otherwise silently drop an injected
        message; back as an inbox item it runs as the next turn instead.
        """
        for item in self.session.take_injections():
            self.enqueue_message(item)

    def _run_worker(self):
        """Own this session's container and process the inbox sequentially.

        A message whose container could not start is never dropped: it goes
        back to the head of the queue (its pill stays honest) and the
        engine bring-up is retried after a pause. The queued pill becomes a
        chat bubble exactly when the message leaves the queue and starts
        its turn."""
        self._start_container()
        try:
            while not self.shutdown.is_set():
                item = self._next_message(timeout=1.0)
                if item is None or item.get("kind") != "message":
                    continue
                item = self._merge_notice_batch(item)
                if item is None:
                    continue
                if self.container is None:
                    self._start_container()
                if self.container is None:
                    self._requeue_front(item)
                    self.shutdown.wait(5.0)
                    continue
                if item.get("was_queued"):
                    self.push(
                        {
                            "type": "user_message",
                            "content": item["content"],
                            "id": item.get("id"),
                        }
                    )
                self.session.clear_cancel()
                self._run_turn(item["content"], bool(item.get("allegro")))
                self._requeue_injections()
        finally:
            unregister_container()
            if self.container is not None:
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

    def _start_container(self):
        """Create the session container, reporting failures as an event.

        The starting flag lets a WebSocket connecting mid-initialization
        show the status pill: the event pushed here races the
        subscriber's registration (the worker starts before the
        WebSocket handler registers), so it is not relied upon for the
        first connection. Non-persistent, so reconnects never replay a
        stale status; the connect path re-pushes it while it is true.

        The worker stays alive when creation fails, so the user's next
        message retries bringing the engine up instead of the session
        going silent forever.
        """
        self.container_starting = True
        self.push({"type": "status", "text": CONTAINER_STARTING_TEXT}, persistent=False)
        try:
            self.container = create_container(shared_copy_sink=self._on_shared_copy)
        except Exception as exc:
            logger.exception("Container creation failed for session %s", self.client_id)
            self.container_starting = False
            self.push({"type": "error", "text": f"Could not start container: {exc}"})
            return
        register_container(self.container)
        registry = processes.registry_for(self.container)
        registry.event_sink = self._on_subagent_event
        registry.completion_sink = self._on_background_completion
        self.container_ready.set()
        self.container_starting = False
        logger.info("Container ready for session %s", self.client_id)
        self.push({"type": "ready"}, persistent=False)

    def _on_shared_copy(self, report: dict):
        """Record the pod-init shared copy as persistent chat history.

        Persistent on purpose: non-persistent pushes race the first
        WebSocket's subscriber registration and would be lost, while
        this one is replayed as history by every client that connects
        to the session.
        """
        self.push({"type": "info", "text": shared_copy_notice(report)})

    def _run_turn(self, user_text: str, allegro: bool = False):
        self.busy.set()
        self.push({"type": "busy"}, persistent=False)
        interaction.register_responder(self._answer_question)
        try:
            self.session.set_allegro(allegro)
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


def _inbox_item_from_message(data: dict) -> dict | None:
    """Translate a websocket message event into an inbox item.

    The Allegro flag is captured here, at enqueue time: the worker later
    samples it when dequeuing, so a turn always runs with the mode the
    user saw when the message left the composer. The client-generated id
    rides along so queue pills and chat bubbles can be matched per
    message. Empty content yields None and must not enqueue anything.
    """
    content = str(data.get("content", "")).strip()
    if not content:
        return None
    return {
        "kind": "message",
        "id": str(data.get("id", "")) or uuid.uuid4().hex,
        "content": content,
        "allegro": bool(data.get("allegro")),
    }


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


def _server_port() -> int:
    """The loopback port the web UI is served on."""
    return int(config.get("server_port", 8000))


# Cross-origin access is pinned to this server's own UI origin: a page
# served from any other local port is a different application and must
# not be able to fetch the HTML shell (which embeds the auth token) or
# open the WebSocket.
TRUSTED_ORIGINS = [
    f"http://{host}:{_server_port()}" for host in ("127.0.0.1", "localhost", "[::1]")
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=TRUSTED_ORIGINS,
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
    """Whether an Origin header names this server's own UI origin.

    Pinned to the configured port, unlike the old any-loopback-port
    regex: another local port hosts some other application, whose pages
    must not read the token-embedding HTML shell or reach the WebSocket.
    """
    if not origin:
        return True
    return origin.lower() in TRUSTED_ORIGINS


@app.middleware("http")
async def enforce_local_origin(request: Request, call_next):
    """Reject requests that did not come from the trusted local UI.

    With a token in force: the Host header must name the server's own
    loopback address, and /api and /setup endpoints must present the
    token (header or query: query because <img> and download links
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


@app.get("/health")
def health():
    """Liveness probe for external monitors and the wrapping app."""
    return {"status": "ok"}


@app.get("/")
def index():
    """Serve the chat GUI with runtime settings injected.

    The config payload escapes `</` so no injected value can close the
    script tag early; AUTH_TOKEN is url-safe anyway, but the config
    shape is user-editable."""
    html = read_asset_text("chat.html")
    gui_config = {
        "max_visible_tool_calls": config.get("max_visible_tool_calls", 3),
        "voice": speech.client_configuration(speech_settings, config),
        "auth_token": AUTH_TOKEN,
    }
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
        key_prefix = preset.get("key_prefix", "")
        if (
            key_prefix
            and request.api_key.strip()
            and not request.api_key.strip().startswith(key_prefix)
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"That does not look like a {preset['name']} key"
                    f" ({preset['name']} keys start with {key_prefix})."
                    " Paste the full key, without extra text."
                ),
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
    still stuck past the stop timeout, teardown proceeds anyway; the old
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


@app.get("/api/background/{client_id}/process/{process_id}/output")
def background_process_output(
    client_id: str,
    process_id: str,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
):
    """Incremental stdout/stderr chunks for the process viewer modal.

    The client polls with the byte offsets it has already shown and
    receives base64 chunks beyond them plus the current sizes and the
    process state. Tail runs before the size probe on purpose: the size
    then covers exactly the bytes delivered, so no chunk is ever
    duplicated on the next poll. Base64 keeps the chunk framing intact
    whatever the process prints.
    """
    record = _sessions.get(client_id)
    if record is None or record.container is None:
        raise HTTPException(status_code=404, detail="Unknown session.")
    registry = processes.registry_for(record.container)
    with registry.lock:
        proc = registry.processes.get(process_id)
    if proc is None:
        raise HTTPException(status_code=404, detail="Unknown process.")
    dir_q = shlex.quote(proc.dir)
    script = "\n".join(
        [
            f"dir={dir_q}",
            f'tail -c +{max(0, stdout_offset) + 1} "$dir/stdout" 2>/dev/null | base64 -w0 || true',
            "printf '\\n'",
            'printf \'%s\\n\' "$(wc -c < "$dir/stdout" 2>/dev/null || echo 0)"',
            f'tail -c +{max(0, stderr_offset) + 1} "$dir/stderr" 2>/dev/null | base64 -w0 || true',
            "printf '\\n'",
            'printf \'%s\\n\' "$(wc -c < "$dir/stderr" 2>/dev/null || echo 0)"',
        ]
    )
    try:
        view = processes.pod_exec(record.container, ["bash", "-c", script])
    except Exception as exc:
        logger.exception("Process output read failed for '%s'", process_id)
        raise HTTPException(
            status_code=502, detail=f"Could not read the process output: {exc}"
        ) from exc
    lines = view.split("\n")
    padded = (lines + ["", "", "", ""])[:4]
    stdout_b64, stdout_size, stderr_b64, stderr_size = (line.strip() for line in padded)
    return {
        "state": proc.state,
        "exit_code": proc.exit_code,
        "killed_by": proc.killed_by,
        "stdout_size": int(stdout_size or 0),
        "stderr_size": int(stderr_size or 0),
        "stdout_b64": stdout_b64,
        "stderr_b64": stderr_b64,
    }


@app.get("/api/background/{client_id}/list")
def background_list(client_id: str):
    """Every registered background work record, oldest spawn first.

    Feeds the top-bar indicator and its list modal: the button is
    visible while at least one process or subagent is running, and the
    modal lists running work first, then finished work.
    """
    record = _sessions.get(client_id)
    if record is None or record.container is None:
        raise HTTPException(status_code=404, detail="Unknown session.")
    registry = processes.registry_for(record.container)
    with registry.lock:
        process_rows = [
            {
                "name": proc.id,
                "state": proc.state,
                "started": proc.started,
                "command": proc.command,
                "exit_code": proc.exit_code,
            }
            for proc in sorted(registry.processes.values(), key=lambda p: p.started)
        ]
        subagent_rows = [
            {
                "name": sub.id,
                "state": sub.state,
                "started": sub.started,
                "goal": sub.goal,
                "level": sub.level,
                "end_reason": sub.end_reason,
                "stopped_by": sub.stopped_by,
            }
            for sub in sorted(registry.subagents.values(), key=lambda s: s.started)
        ]
    return {"processes": process_rows, "subagents": subagent_rows}


@app.get("/api/background/{client_id}/subagent/{subagent_id}/events")
def background_subagent_events(client_id: str, subagent_id: str):
    """The subagent viewer's history: goal, state, and streamed events."""
    record = _sessions.get(client_id)
    if record is None or record.container is None:
        raise HTTPException(status_code=404, detail="Unknown session.")
    registry = processes.registry_for(record.container)
    with registry.lock:
        record_subagent = registry.subagents.get(subagent_id)
    if record_subagent is None:
        raise HTTPException(status_code=404, detail="Unknown subagent.")
    with record_subagent.events_lock:
        events = list(record_subagent.events)
    return {
        "id": subagent_id,
        "goal": record_subagent.goal,
        "level": record_subagent.level,
        "state": record_subagent.state,
        "end_reason": record_subagent.end_reason,
        "stopped_by": record_subagent.stopped_by,
        "events": events,
    }


@app.get("/api/image/{client_id}")
def serve_pod_image(client_id: str, path: str, download: bool = False):
    """Stream an image from the session pod for inline rendering.

    The client rewrites markdown image paths (e.g. from Syna's replies)
    to this endpoint. Only image files under /workspace are eligible;
    everything is copied out of the pod, so host files are unreachable.
    With `download` (the UI's per-image download button), the response
    carries a Content-Disposition attachment header.
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
    return _prepare_pod_image(record, cleaned, as_attachment=download)


def _prepare_pod_image(
    record, pod_path: str, as_attachment: bool = False
) -> FileResponse:
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
        pod_copy_from(record.container, pod_path, str(token_dir))
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
                        record,
                        token_dir,
                        source,
                        _media_type_for(pod_path),
                        download_name=basename(pod_path) if as_attachment else None,
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
    return _serve_image_copy(
        record,
        token_dir,
        target,
        "image/png",
        download_name=basename(pod_path) if as_attachment else None,
    )


def _media_type_for(pod_path: str) -> str:
    """Map a validated pod image path to its response media type."""
    return SERVED_IMAGE_EXTENSIONS[Path(pod_path).suffix.lower()]


def _content_disposition(download_name: str) -> str:
    """Build a header-safe Content-Disposition attachment value.

    HTTP headers are latin-1 encoded by Starlette, so names carrying
    characters above U+00FF (CJK, Cyrillic, Greek, ...) would raise
    UnicodeEncodeError inside the response and break the download; those
    go into the RFC 5987 `filename*` form percent-encoded as UTF-8,
    which every modern browser prefers. Control characters and quotes
    are stripped in both forms: a pod filename the model controls must
    not reach the HTTP header layer unfiltered.
    """
    cleaned = "".join(
        character
        for character in download_name.replace('"', "").replace("\\", "")
        if character >= " " and character != chr(127)
    ).strip()
    if not cleaned:
        cleaned = "image.png"
    try:
        cleaned.encode("latin-1")
    except UnicodeEncodeError:
        encoded = quote(cleaned.encode("utf-8"))
        return f"attachment; filename=\"image.png\"; filename*=UTF-8''{encoded}"
    return f'attachment; filename="{cleaned}"'


def _serve_image_copy(
    record,
    token_dir: Path,
    file_path: Path,
    media_type: str,
    download_name: str | None = None,
) -> FileResponse:
    """Track a prepared copy for eviction and build its file response.

    The LRU cap bounds the temp footprint across history replays; the
    session tmp dir wipe on teardown removes whatever remains. When
    `download_name` is set the response is served as an attachment.
    """
    record.image_cache[uuid.uuid4().hex] = token_dir
    while len(record.image_cache) > IMAGE_CACHE_LIMIT:
        oldest_key = next(iter(record.image_cache))
        oldest_dir = record.image_cache.pop(oldest_key)
        shutil.rmtree(oldest_dir, ignore_errors=True)
    headers = {"Cache-Control": "no-store"}
    if download_name:
        headers["Content-Disposition"] = _content_disposition(download_name)
    return FileResponse(
        file_path,
        media_type=media_type,
        headers=headers,
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
        "continuous_conversation": DEFAULT_CONTINUOUS_CONVERSATION,
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
            "continuous_conversation": runtime_cfg.get(
                "continuous_conversation",
                defaults["continuous_conversation"],
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


async def _pump_events(subscriber: queue.Queue, websocket: WebSocket):
    """Forward one connection's queued session events to its WebSocket."""
    while True:
        try:
            event = await asyncio.to_thread(subscriber.get, True, 1.0)
        except queue.Empty:
            continue
        try:
            await websocket.send_json(event)
        except Exception:
            return


def _kill_background(record: SessionRecord, message_type: str, background_id: str):
    """Run a modal's stop request off the event loop.

    A kill talks to the pod (signals, artifact copies), which can take
    seconds; blocking the WebSocket receive loop would stall every
    other client message. Failures surface as error events.
    """

    def run():
        try:
            if record.container is None:
                raise RuntimeError("the pod is not running")
            if message_type == "process_kill":
                processes.kill_process(
                    background_id, container=record.container, by="user"
                )
            else:
                subagents.kill_subagent(
                    background_id, container=record.container, by="user"
                )
        except Exception as exc:
            logger.exception("Modal kill failed for '%s'", background_id)
            record.push(
                {
                    "type": "error",
                    "text": f"Could not stop '{background_id}': {exc}",
                }
            )

    threading.Thread(target=run, name="syna-modal-kill", daemon=True).start()


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """Two-way realtime channel between one client and its session.

    With a token in force, origin and token checks run before accept:
    browsers do not same-origin restrict WebSockets, so the server
    enforces it; a missing Origin comes from the file:// fallback
    client and still needs the token, and closing before accepting
    denies the handshake with an HTTP 403. The subscriber registers
    under the same lock push() holds, so any event pushed from that
    point lands in this connection's queue, and the history snapshot
    carries everything before it, exactly once, no gap."""
    if AUTH_TOKEN:
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
    record.touch()

    subscriber: queue.Queue = queue.Queue()
    with record.event_log_lock:
        history = list(record.event_log)
        record.subscribers.append(subscriber)
        record.connected = True
    try:
        for event in history:
            await websocket.send_json(event)
        await websocket.send_json({"type": "history_done"})
        # The replayed backlog ends on the last persisted event, which is
        # often a "Thinking..." status notice: busy/ready are pushed
        # non-persistent by design, so nothing in the log would ever
        # clear the pill after a reload. One authoritative live event
        # settles the client on the worker's actual turn state.
        await websocket.send_json({"type": "busy" if record.busy.is_set() else "ready"})
    except Exception:
        with record.event_log_lock:
            if subscriber in record.subscribers:
                record.subscribers.remove(subscriber)
            record.connected = bool(record.subscribers)
        return

    pump_task = asyncio.create_task(_pump_events(subscriber, websocket))
    if record.container_starting:
        record.push(
            {"type": "status", "text": CONTAINER_STARTING_TEXT}, persistent=False
        )
    try:
        while True:
            data = await websocket.receive_json()
            record.touch()
            message_type = data.get("type")
            if message_type == "message":
                item = _inbox_item_from_message(data)
                if item is None:
                    continue
                content = item["content"]
                if record.pending_question.active and record.answer_pending_question(
                    content
                ):
                    record.push({"type": "user_message", "content": content})
                    continue
                was_queued = record.busy.is_set()
                item["was_queued"] = was_queued
                record.push(
                    {
                        "type": "user_message",
                        "content": content,
                        "id": item.get("id"),
                        "queued": was_queued,
                    }
                )
                record.enqueue_message(item)
            elif message_type == "send_now":
                item_id = str(data.get("id", ""))
                if not item_id:
                    continue
                item = record.take_message(item_id)
                if item is None:
                    continue
                if record.busy.is_set():
                    # Deliver at the turn's next request boundary; the
                    # session never interrupts what it is doing.
                    record.session.queue_injection(item)
                else:
                    record.enqueue_message(item)
            elif message_type == "answer":
                choice = str(data.get("choice", "")).strip()
                if not choice:
                    continue
                if record.answer_pending_question(choice):
                    record.push({"type": "user_message", "content": choice})
            elif message_type == "stop":
                record.session.cancel()
                record.release_pending_question()
            elif message_type in ("process_kill", "subagent_kill"):
                background_id = str(data.get("id", "")).strip()
                if background_id:
                    _kill_background(record, message_type, background_id)
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        with record.event_log_lock:
            if subscriber in record.subscribers:
                record.subscribers.remove(subscriber)
            record.connected = bool(record.subscribers)
        record.touch()


def main():
    """Start the Syna web server."""
    global AUTH_TOKEN
    AUTH_TOKEN = secrets.token_urlsafe(32)
    port = _server_port()
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
