"""Verbatim read-only archives of large user messages inside the pod.

When a user message carries a large amount of data meant to be used
verbatim, retyping it from memory corrupts it. Such messages are written
to /user_messages/NNNNNN.txt in the pod (mode 444) so the model can pipe
the exact bytes into scripts. The history copy of an archived message
carries a marker with its archive path; the context elision pass
(context.py) uses that marker to replace old archived messages with a
head excerpt plus the pointer.
"""

import re
import shlex
import tempfile
from pathlib import Path

from ..config import (
    get_logger,
    load_user_messages_dest,
)
from ..pod_io import pod_copy_to, pod_exec

USER_MESSAGE_MARKER_TEMPLATE = (
    "[Harness note: a verbatim, read-only copy of this message is archived"
    " in the pod at {path} (archive id {nonce}). Pipe that file into"
    " scripts instead of retyping its contents.]"
)

# The archive id is a per-session random nonce: the elision pass only
# treats a marker as genuine when it carries the matching id, so user
# text that merely quotes this format cannot forge an archive pointer.
ARCHIVE_MARKER_PATTERN = re.compile(
    r"\[Harness note: a verbatim, read-only copy of this message is"
    r" archived in the pod at (?P<path>\S+?\.txt)"
    r" \(archive id (?P<nonce>[0-9a-f]+)\)\."
)


def archive_user_message(text: str, container, seq: int) -> str | None:
    """Write one user message into the pod read-only; return its path.

    Uses the same host-temporary-file-and-copy mechanism as the other pod
    writes. Any failure is logged and reported as None so a broken archive
    never blocks the turn; the message then travels unannotated, exactly
    as it would without this module. The copy is written as bytes, not
    with `write_text`: text mode would translate line feeds to
    carriage-return pairs on Windows and corrupt the verbatim archive.
    """
    logger = get_logger()
    root = load_user_messages_dest()
    file_name = f"{seq:06d}.txt"
    pod_path = f"{root.rstrip('/')}/{file_name}"
    try:
        pod_exec(container, ["bash", "-c", f"mkdir -p {shlex.quote(root)}"])
        with tempfile.TemporaryDirectory(prefix="syna-archive-") as temp_dir:
            host_file = Path(temp_dir, file_name)
            host_file.write_bytes(text.encode("utf-8"))
            pod_copy_to(container, str(host_file), root)
        pod_exec(container, ["bash", "-c", f"chmod 444 {shlex.quote(pod_path)}"])
    except Exception:
        logger.exception("Failed to archive a user message to %s", pod_path)
        return None
    logger.info("Archived user message %d at %s (%d chars)", seq, pod_path, len(text))
    return pod_path
