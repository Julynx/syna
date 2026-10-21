"""User-interaction bridge between the ask_user tool and host interfaces.

Tools never touch sessions, servers, or terminals directly. Before running
a turn, a host interface (web server, CLI) registers a responder for the
thread that will execute the turn; parse.py propagates it into the tool
worker thread the same way it propagates the container binding. The
ask_user tool calls ask() and blocks until the responder returns the
user's answer, or until the task is stopped (InteractionCancelled).
"""

import queue
import threading


class InteractionError(Exception):
    """Raised when no responder is attached to carry a question."""


class InteractionCancelled(Exception):
    """Raised when the user stopped the task before answering a question."""


_local = threading.local()


def register_responder(responder) -> None:
    """Bind the question responder to the calling thread."""
    _local.responder = responder


def unregister_responder() -> None:
    """Unbind the calling thread's responder."""
    _local.responder = None


def get_responder():
    """Return the responder bound to the calling thread, if any."""
    return getattr(_local, "responder", None)


def ask(question_text: str, choices: list[str]) -> str:
    """Ask the user a question and block until an answer arrives.

    Raises InteractionError when the calling context has no responder
    (non-interactive runs), and InteractionCancelled when the task was
    stopped while the question was pending.
    """
    responder = get_responder()
    if responder is None:
        raise InteractionError("No interactive user is attached to this session.")
    return responder(question_text, choices)


class PendingQuestion:
    """State of one ask_user question awaiting the user's answer.

    The session's worker thread opens the question and blocks on the
    returned answer queue; the interface's own threads (WebSocket
    handler, signal handlers) resolve or release it from outside.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._answers: queue.Queue | None = None

    def open(self) -> queue.Queue:
        """Register a pending question and return its answer queue."""
        with self._lock:
            self._answers = queue.Queue()
            return self._answers

    def resolve(self, answer: str | None) -> bool:
        """Deliver an answer (None marks a cancellation).

        Returns False when no question is pending, so a stale answer
        arriving after a release is ignored instead of queued.
        """
        with self._lock:
            answers = self._answers
            self._answers = None
        if answers is None:
            return False
        answers.put(answer)
        return True

    @property
    def active(self) -> bool:
        """Whether a question is currently waiting for an answer."""
        with self._lock:
            return self._answers is not None
