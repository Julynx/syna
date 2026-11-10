"""Runtime configuration and logging setup for Syna."""

import json
import logging
import math
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .paths import asset_path, get_data_home, read_asset_text

DEFAULT_COMMAND_TIMEOUT_S = 50
DEFAULT_TOOL_CALL_TIMEOUT_S = 60
DEFAULT_OUTPUT_LIMIT_CHARS = 40000

# Timeout budgets for the browser-backed capabilities (web search and
# the browser tool). Deliberately not subject to the Allegro
# scale: starting a stealth browser from cold (process spawn,
# fingerprint setup, cold OS file caches) and a slow search engine do
# not get faster because the user asked the agent to move quickly, and
# a budget that halves with the mode reintroduces the cold-start race
# these budgets exist to prevent. The command budget bounds one browser
# tool command in aggregate (a visit's worst legitimate path is a 30 s
# navigation plus challenge waiting, so 90 s keeps late successes).
DEFAULT_WEB_SEARCH_TIMEOUT_S = 60
DEFAULT_BROWSER_LAUNCH_TIMEOUT_S = 120
DEFAULT_BROWSER_COMMAND_TIMEOUT_S = 90
# How long the harness waits after asking a call to stop before
# detaching from it: the SIGTERM-to-SIGKILL grace.
DEFAULT_MAX_CANCEL_WAIT_S = 10

# The stealth browser's DNS-over-HTTPS resolver. Quad9's unfiltered
# endpoint by default: it bypasses DNS poisoning used by national
# content filters, and Firefox reads HTTPS DNS records through it,
# which upgrades navigation to HTTP/3 where the site offers it
# (bypassing TCP SNI filtering) and applies ECH configs where
# published. TRR mode 2 keeps native DNS as a fallback when the
# resolver is unreachable. Empty disables DoH. The bootstrap IP
# resolves the resolver's own hostname so a poisoning middlebox
# cannot redirect the DoH connection itself. The filtered 9.9.9.9
# endpoint is deliberately avoided: its threat lists block domains
# (Anna's Archive and others) that filtering opponents need.
DEFAULT_BROWSER_DOH_URI = "https://dns10.quad9.net/dns-query"
DEFAULT_BROWSER_DOH_BOOTSTRAP = "9.9.9.10"

# The pod resolves DNS over HTTPS through the same unfiltered resolver,
# so shell tools (curl, pip, apt, git) are immune to resolver poisoning
# too; the bootstrap runs once on container creation and fails open.
DEFAULT_POD_DOH_URI = DEFAULT_BROWSER_DOH_URI
DEFAULT_POD_DOH_BOOTSTRAP = DEFAULT_BROWSER_DOH_BOOTSTRAP
# Secondary bootstrap IP on the same provider, registered as a second
# resolver entry so dnscrypt-proxy fails over when the primary anycast
# address is unreachable. Must belong to the provider in pod_doh_uri.
DEFAULT_POD_DOH_BOOTSTRAP_ALT = "149.112.112.10"

# Canonical provider model defaults. The first-run wizard, the settings
# modal, and the runtime all read these; an unset .env value means the
# default applies.
DEFAULT_MODEL = "~z-ai/glm-flash-latest"
DEFAULT_STT_MODEL = "google/chirp-3"
DEFAULT_TTS_MODEL = "google/gemini-3.8-flash-lite-tts"
DEFAULT_TTS_VOICE = "Kore"
DEFAULT_IMAGE_MODEL = "google/gemini-3.1-flash-image"

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def normalize_base_url(base_url: str | None) -> str:
    """Return a BASE_URL value stripped, defaulted, and slash-trimmed.

    Unset or empty falls back to OpenRouter; the trailing slash is
    stripped so URL joins never produce double slashes.
    """
    return ((base_url or "").strip() or DEFAULT_OPENROUTER_BASE_URL).rstrip("/")


def is_openrouter_base_url(base_url: str | None) -> bool:
    """Whether a BASE_URL value points at OpenRouter itself.

    OpenRouter accepts its own top-level `reasoning` object; other
    OpenAI-compatible endpoints may reject it as an unknown parameter,
    so OpenRouter-only request fields are gated on this check.
    """
    return normalize_base_url(base_url) == DEFAULT_OPENROUTER_BASE_URL


def sticky_routing_fields(session_id: str | None) -> dict:
    """Return the request-body fields that pin provider cache routing.

    OpenRouter accepts a top-level `session_id` as its sticky-routing
    key, keeping every request of a conversation on the provider that
    holds the warm prompt cache. OpenAI's documented equivalent is
    `prompt_cache_key`. Strict OpenAI-compatible endpoints may reject
    unknown top-level fields with a 400 (the same failure mode the
    ungated `reasoning` object had), so other custom BASE_URLs get
    neither field and rely on the providers' automatic prefix caching.
    """
    if not session_id:
        return {}
    base_url = normalize_base_url(get_env().get("BASE_URL"))
    if base_url == DEFAULT_OPENROUTER_BASE_URL:
        return {"session_id": session_id}
    if base_url == DEFAULT_OPENAI_BASE_URL:
        return {"prompt_cache_key": session_id}
    return {}


# Feature toggles edited in the settings modal. Voice stays available with
# default models but voice mode itself starts off; image generation is
# opt-in because it is expensive and model-initiated.
DEFAULT_VOICE_ENABLED = True
DEFAULT_IMAGE_GENERATION_ENABLED = False
# Hands-free voice mode: the client reopens the microphone after each
# reply. Opt-in like voice mode itself.
DEFAULT_CONTINUOUS_CONVERSATION = False


def load_output_limit_chars() -> int:
    """Return the character limit at which tool output is cut to head and tail."""
    return load_config().get("max_command_output_chars", DEFAULT_OUTPUT_LIMIT_CHARS)


@lru_cache(maxsize=1)
def get_env() -> dict[str, str]:
    """Read the .env file in the Syna data home and return its values."""
    return dotenv_values(get_data_home() / ".env")


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Load configuration values from config.json in the Syna data home.

    The data home is seeded with the bundled defaults on first run. To
    keep existing installs working across upgrades, the bundled defaults
    are merged under the user's file: keys the user's file lacks appear
    with their default values (an old config.json predating a new
    required key no longer crashes callers that index directly), and
    every user-set value wins. The configuration is cached after the
    first read; treat the returned dict as read-only and expect a
    restart to pick up edits.
    """
    config_path = get_data_home() / "config.json"
    with open(config_path, encoding="utf-8") as config_file:
        user_config = json.load(config_file)
    return _merge_values(_bundled_defaults(), user_config)


def _bundled_defaults() -> dict[str, Any]:
    """The shipped config.json defaults; empty when they cannot be read."""
    try:
        with open(asset_path("config.json"), encoding="utf-8") as defaults_file:
            return json.load(defaults_file)
    except (OSError, ValueError):
        return {}


def _merge_values(defaults: Any, override: Any) -> Any:
    """Merge two config values recursively; the override wins."""
    if isinstance(defaults, dict) and isinstance(override, dict):
        merged = dict(defaults)
        for key, value in override.items():
            merged[key] = _merge_values(merged.get(key), value)
        return merged
    return override


def save_config(updates: dict[str, Any]) -> Path:
    """Merge updates into config.json in the Syna data home and clear cache."""
    config_path = get_data_home() / "config.json"
    current = {}
    if config_path.is_file():
        try:
            with open(config_path, "r", encoding="utf-8") as config_file:
                current = json.load(config_file)
        except Exception:
            current = {}
    current.update(updates)
    with open(config_path, "w", encoding="utf-8") as config_file:
        json.dump(current, config_file, indent=2)
    load_config.cache_clear()
    return config_path


def get_logger() -> logging.Logger:
    """Configure file-based logging for the application and return the logger."""
    config = load_config()
    log_file_name = config.get("log_file", "syna.log")
    log_file_path = get_data_home() / log_file_name

    logger = logging.getLogger("syna")
    if not logger.handlers:
        file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)

    return logger


def get_session_dump_path(session_id: str | None = None) -> Path:
    """Get the path to the session dump file, where the messages array will be saved.

    When 'session_id' is given, it is woven into the file name (e.g.
    'message-session-log-<id>.json') so that concurrent sessions in server
    mode never overwrite each other's dumps. The id is sanitized to safe
    file-name characters and capped in length first.
    """
    config = load_config()
    dump_file_name = config.get("session_dump_file", "message-session-log.json")
    if session_id:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", str(session_id))[:64]
        configured_path = Path(dump_file_name)
        suffix = configured_path.suffix or ".json"
        dump_file_name = f"{configured_path.stem}-{safe_id}{suffix}"
    return get_data_home() / dump_file_name


def load_command_timeout_s() -> int:
    """Return the per-command timeout in seconds from the configuration.

    The active thread's Allegro scale (set_timeout_scale) applies: the
    session worker runs a whole turn on one thread, so the halved value
    is enforced exactly for the duration of an Allegro turn and never
    leaks into concurrent sessions on other threads.
    """
    return _apply_timeout_scale(
        load_config().get("command_timeout_s", DEFAULT_COMMAND_TIMEOUT_S)
    )


def load_tool_call_timeout_s() -> float:
    """Return the per-tool-call timeout in seconds from the configuration.

    Subject to the calling thread's Allegro scale, like
    load_command_timeout_s.
    """
    return _apply_timeout_scale(
        load_config().get("tool_call_timeout_s", DEFAULT_TOOL_CALL_TIMEOUT_S)
    )


_timeout_scale = threading.local()

ALLEGRO_TIMEOUT_SCALE = 0.5
MIN_SCALED_TIMEOUT_S = 1


def set_timeout_scale(scale: float | None) -> None:
    """Set the calling thread's multiplicative timeout scale.

    Called by the session around a turn: 0.5 halves the command and
    tool-call timeouts for Allegro mode. None clears the override. The
    scale is thread-local by design; other sessions on other threads
    keep their configured timeouts.
    """
    _timeout_scale.scale = scale


def scale_seconds(seconds: float, scale: float) -> float:
    """Scale a timeout, floored at one second.

    Half-up rounding for whole-second timeouts, matching the web UI's
    Math.round so the settings note and the enforcement layer agree on
    odd configured values. Integer inputs return integers.
    """
    scaled = max(MIN_SCALED_TIMEOUT_S, seconds * scale)
    return math.floor(scaled + 0.5) if isinstance(seconds, int) else scaled


def _apply_timeout_scale(seconds):
    """Apply the calling thread's timeout scale, floored at one second."""
    scale = getattr(_timeout_scale, "scale", None)
    if not scale or scale == 1:
        return seconds
    return scale_seconds(seconds, scale)


def load_web_search_timeout_s() -> float:
    """Return the budget for one web search's engine chain in seconds.

    Not subject to the Allegro scale (see the module defaults).
    """
    return load_config().get("web_search_timeout_s", DEFAULT_WEB_SEARCH_TIMEOUT_S)


def load_browser_launch_timeout_s() -> float:
    """Return the budget for starting a shared stealth browser.

    Applies to the search browser and the tool browser alike. Not
    subject to the Allegro scale (see the module defaults).
    """
    return load_config().get(
        "browser_launch_timeout_s", DEFAULT_BROWSER_LAUNCH_TIMEOUT_S
    )


def load_browser_command_timeout_s() -> float:
    """Return the aggregate budget for one browser tool command.

    Not subject to the Allegro scale (see the module defaults).
    """
    return load_config().get(
        "browser_command_timeout_s", DEFAULT_BROWSER_COMMAND_TIMEOUT_S
    )


def load_max_cancel_wait_s() -> float:
    """Return how long the harness waits for a stopped call to exit."""
    return load_config().get("max_cancel_wait_s", DEFAULT_MAX_CANCEL_WAIT_S)


def load_browser_doh_uri() -> str:
    """Return the browser's DoH resolver URI; empty disables DoH."""
    return load_config().get("browser_doh_uri", DEFAULT_BROWSER_DOH_URI)


def load_browser_doh_bootstrap() -> str:
    """Return the IP that bootstraps resolution of the DoH resolver name."""
    return load_config().get("browser_doh_bootstrap", DEFAULT_BROWSER_DOH_BOOTSTRAP)


def load_pod_doh_uri() -> str:
    """Return the pod's DoH resolver URI; empty leaves the pod on native DNS."""
    return load_config().get("pod_doh_uri", DEFAULT_POD_DOH_URI)


def load_pod_doh_bootstrap() -> str:
    """Return the IP pinning the pod's DoH connection away from poisoned DNS."""
    return load_config().get("pod_doh_bootstrap", DEFAULT_POD_DOH_BOOTSTRAP)


def load_pod_doh_bootstrap_alt() -> str:
    """Return the secondary bootstrap IP for the pod's resolver failover."""
    return load_config().get("pod_doh_bootstrap_alt", DEFAULT_POD_DOH_BOOTSTRAP_ALT)


DEFAULT_USER_MESSAGE_ARCHIVE_MIN_CHARS = 2000
DEFAULT_USER_MESSAGES_DEST = "/user_messages"
DEFAULT_ELIDE_USER_MESSAGE_HEAD_CHARS = 300
DEFAULT_ELIDE_MIN_CHARS = 2000
DEFAULT_VERIFY_LOG_TAIL_CHARS = 128000

# Background work. Processes and subagents get their own runtime caps,
# independent of the per-call tool timeout: a detached process or
# subagent outlives the tool call that spawned it. Subagent depth counts
# nesting levels including main, so 3 = main -> subagent ->
# sub-subagent, and a sub-subagent cannot spawn further. The concurrent
# cap bounds API spend; wait_for follows its own deadline, not
# tool_call_timeout_s. Tail sizes are measured in characters.
DEFAULT_PROCESS_MAX_RUNTIME_S = 1200
DEFAULT_SUBAGENT_MAX_RUNTIME_S = 1800
DEFAULT_MAX_SUBAGENT_DEPTH = 3
DEFAULT_MAX_CONCURRENT_SUBAGENTS = 4
DEFAULT_WAIT_FOR_TIMEOUT = 120
DEFAULT_CHECK_TAIL_CHARS = 4000
DEFAULT_SUBAGENT_TAIL_CHARS = 1200


def load_user_message_archive_min_chars() -> int:
    """Return the message length at which user messages are archived verbatim."""
    return load_config().get(
        "user_message_archive_min_chars", DEFAULT_USER_MESSAGE_ARCHIVE_MIN_CHARS
    )


def load_user_messages_dest() -> str:
    """Return the pod directory holding the archived user messages."""
    return load_config().get("user_messages_dest", DEFAULT_USER_MESSAGES_DEST)


def load_elide_user_message_head_chars() -> int:
    """Return how much of an archived user message stays in history."""
    return load_config().get(
        "elide_user_message_head_chars", DEFAULT_ELIDE_USER_MESSAGE_HEAD_CHARS
    )


def load_elide_min_chars() -> int:
    """Return the output size at which routine elision replaces text with a pointer.

    Eliding a short output is a net loss for the prompt cache: the
    pointer is nearly as long as the text, and rewriting one message
    re-bills the whole suffix after it at the uncached rate. Outputs
    shorter than this stay inline with their saved_to pointer attached.
    The budget-exceeded elision path ignores the threshold.
    """
    return load_config().get("elide_min_chars", DEFAULT_ELIDE_MIN_CHARS)


def load_verify_log_tail_chars() -> int:
    """Return how much of the conversation log the work reviewer receives."""
    return load_config().get("verify_log_tail_chars", DEFAULT_VERIFY_LOG_TAIL_CHARS)


def load_process_max_runtime_s() -> int:
    """Return the runtime cap of one detached pod process."""
    return int(
        load_config().get("process_max_runtime_s", DEFAULT_PROCESS_MAX_RUNTIME_S)
    )


def load_subagent_max_runtime_s() -> int:
    """Return the runtime cap of one subagent turn."""
    return int(
        load_config().get("subagent_max_runtime_s", DEFAULT_SUBAGENT_MAX_RUNTIME_S)
    )


def load_max_subagent_depth() -> int:
    """Return the subagent nesting depth cap, counting main."""
    return int(load_config().get("max_subagent_depth", DEFAULT_MAX_SUBAGENT_DEPTH))


def load_max_concurrent_subagents() -> int:
    """Return how many subagents may run at once."""
    return int(
        load_config().get("max_concurrent_subagents", DEFAULT_MAX_CONCURRENT_SUBAGENTS)
    )


def load_wait_for_timeout() -> int:
    """Return the default deadline of one wait_for call."""
    return int(load_config().get("wait_for_timeout", DEFAULT_WAIT_FOR_TIMEOUT))


def load_check_tail_chars() -> int:
    """Return how many characters of output the check tools tail."""
    return int(load_config().get("check_tail_chars", DEFAULT_CHECK_TAIL_CHARS))


def load_subagent_tail_chars() -> int:
    """Return how many transcript characters a subagent check previews.

    A transcript tail is whole JSON-escaped messages, far denser than a
    process's plain output tail, so it gets its own smaller budget; the
    check text always points at the full transcript path regardless.
    """
    return int(
        load_config().get("subagent_check_tail_chars", DEFAULT_SUBAGENT_TAIL_CHARS)
    )


__all__ = [
    "asset_path",
    "get_data_home",
    "load_browser_launch_timeout_s",
    "load_check_tail_chars",
    "load_command_timeout_s",
    "load_config",
    "load_max_cancel_wait_s",
    "load_max_concurrent_subagents",
    "load_max_subagent_depth",
    "load_output_limit_chars",
    "load_process_max_runtime_s",
    "load_subagent_max_runtime_s",
    "load_subagent_tail_chars",
    "load_tool_call_timeout_s",
    "load_wait_for_timeout",
    "load_web_search_timeout_s",
    "read_asset_text",
    "scale_seconds",
    "set_timeout_scale",
]
