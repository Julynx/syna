# Syna: Technical Description

This document is the complete map of how Syna works, organized by topic
rather than by source file. Chapters 1–4 cover operations, configuration,
and the runtime architecture; chapters 5–9 walk the stack from the wire
protocol to the web server; chapters 10–13 cover the client surfaces,
voice, and testing. The module map in [Architecture](#4-architecture)
locates any source file in one hop.

- [1. Overview](#1-overview)
- [2. Running and operations](#2-running-and-operations)
- [3. Configuration](#3-configuration)
- [4. Architecture](#4-architecture)
- [5. The protocol layer](#5-the-protocol-layer)
- [6. The agent loop](#6-the-agent-loop)
- [7. Capabilities](#7-capabilities)
- [8. The browser stack](#8-the-browser-stack)
- [9. The web server](#9-the-web-server)
- [10. The web UI](#10-the-web-ui)
- [11. Voice and narration](#11-voice-and-narration)
- [12. Testing](#12-testing)
- [13. Website](#13-website)

Maintaining it: new information goes into the most specific chapter, not
the end of the file; a new cross-cutting feature earns its own chapter; a
chapter drifting past ~250 lines is a signal to split it into its own
file under `docs/`.

## 1. Overview

Syna is a terminal and web AI agent that answers user requests by driving an
LLM (through OpenRouter) in a native tool-calling loop. Tool schemas are
generated dynamically from `tools.py` function signatures and docstrings.
When the model invokes tools, the agent executes the requested calls
sequentially; the first failing call stops the batch and remaining calls are
reported as `skipped` so that every `tool_call_id` receives a corresponding
result message. When the model emits no tool calls, its plain text response is
delivered to the user as the final answer.

Tool results reach the model as individual `role: "tool"` messages containing
`<result status=... exit_code=... saved_to=...>` elements with head-and-tail
truncated output; the full output of every call is saved inside the pod at
`/tool_outputs/` and its path is the `saved_to` attribute. Harness-originated
messages (corrective warnings, format errors, step-budget warnings, history
summaries) arrive wrapped in `<harness_notice>` tags. All system-originated
content is additionally framed by sentinel channels (see `channel.py`) so the
model cannot forge it.

Most tools execute inside a per-session Docker/Podman container (image
`python:3.14-trixie`, managed through the `bollard` client), giving the model
a sandboxed Debian machine ("the pod") in which it can run commands and
create files with full root access.

## 2. Running and operations

- Web server & UI: `syna` (entry point `syna.server:main`, aliases: `syna-agent`,
  `syna-server`): FastAPI + WebSocket chat UI served from the bundled `chat.html`.
  Automatically opens the first-run setup wizard if setup is incomplete.
- CLI: `syna-cli` (entry point `syna:main`): interactive prompt `(Ask Syna):`,
  supports slash commands. Exits cleanly with guidance if configuration is missing.
- Setup wizard: `syna-setup` (entry point `syna.setup:main`): starts the
  web server and opens the first-run wizard at `/setup`.
- Web UI sources live in `webui/` (see below); after editing them, regenerate
  the UIs with `python scripts/build.py`: `src/syna/assets/chat.html` and
  `src/syna/assets/setup.html` are generated files and
  must not be edited directly.
- CI (`.github/workflows/tests.yml`): every push and pull request runs the
  offline test suites on Python 3.12 in an isolated `SYNA_HOME` seeded with
  the bundled `config.json` and a dummy API key, skipping the browser and
  live-API suites, then checks `ruff format`.
- Install from PyPI with `uv tool install syna-agent` (or
  `pip install syna-agent`); for development, `uv sync`; Python >= 3.12
  required.

### Filesystem layout

Read-only assets (prompts, config defaults, web UIs, vendor files) are
bundled inside the package at `src/syna/assets/` and loaded through
`paths.py` (`importlib.resources`; `asset_path` / `read_asset_text`).
Everything the user writes lives in the Syna data home (`~/.syna`,
overridable with `SYNA_HOME`): `.env`, `config.json` (seeded from the
bundled defaults on first run), `syna.log`, and session dumps. In a
repository checkout the repo root acts as the data home so development
workflows keep working unchanged.

### Dependencies

- `openrouter`: model client with configurable timeout/retry.
- `python-dotenv`: loads `OPENROUTER_API_KEY` and model settings from `.env`.
- `httpx`: OpenRouter audio endpoints (speech transcription and synthesis).
- `bollard`: Docker/Podman container client used for the sandbox.
- `playwright`, `camoufox`: browser-based web search and the persistent
  stealth browser session.
- `pillow`: image downscaling for the vision pipeline.
- `fastapi`, `uvicorn`, `python-multipart`: web server and uploads.

### `src/syna/paths.py`

The filesystem layout module: `get_project_root()` (repo root, checkout
fallback), `get_data_home()` (`$SYNA_HOME` → repo root → `~/.syna`,
created and seeded with the bundled `config.json` on first run),
`asset_path(name)` / `read_asset_text(name)` for the bundled package
assets (real paths on disk; entered-and-kept contexts for zipped
installs).

### `src/syna/setup.py`

First-run setup, shared by `syna-server` (which redirects `/` to the
wizard while setup is incomplete) and the `syna-setup` entry point.
Detection returns data, never raises: `detect_wsl` / `detect_podman` /
`detect_docker` / `detect_engine` (PATH probes plus `--version`). On
Windows the engine probes call `_refresh_path()` first, which merges the
registry PATH (user + machine) into the running process so an engine
installed mid-session is detected without an app restart. Installation is
best-effort per platform (`winget` on Windows, package managers
elsewhere) with the log tail returned for the wizard's error box; WSL
reports `reboot_required`. On Windows the engine installers verify the
outcome with a fresh detection (`_windows_engine_install`) instead of
trusting winget's exit code, which is non-zero for the localized
"already installed, no update" case. `write_env_file` merges keys into the data
home `.env` and clears the env cache; `endpoint_needs_key` exempts
local endpoints; `setup_incomplete()` (checked live on every request,
so finishing the wizard flips the server in-process) requires a key
unless local, an engine, and treats a missing MODEL as satisfied since
the runtime falls back to `config.DEFAULT_MODEL`; `sanity_check()` runs
the engine probe, brings the engine endpoint up through
`state.ensure_engine_ready()` (reporting a failure as data instead of
letting the user into the chat), and a one-token chat completion against
the configured endpoint. `PROVIDERS` holds the preset base URLs, key-page links, and
default models (OpenRouter, OpenAI, Google Gemini, Ollama), sourced
from the canonical constants in `config.py`.

## 3. Configuration

`.env` holds the secrets and model selection:

| Key                     | Purpose                                                                                                                                                                                                                                                                                                                                                              |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `OPENROUTER_API_KEY`    | API key for the configured endpoint (any OpenAI-compatible provider's key goes here; the variable name predates multi-endpoint support).                                                                                                                                                                                                                             |
| `MODEL`                 | Main agent model (default `~z-ai/glm-flash-latest` when unset).                                                                                                                                                                                                                                                                                                      |
| `THINKING_EFFORT`       | Reasoning effort for the main agent requests. Unset, the default `medium` applies on OpenRouter; on other endpoints unset omits the field entirely (the OpenRouter-style `reasoning` object is forwarded verbatim to custom `BASE_URL`s and strict OpenAI-compatible endpoints reject it as an unknown parameter). An explicit value is always sent to any endpoint. |
| `BASE_URL`              | OpenAI-like endpoint URL (default `https://openrouter.ai/api/v1`; also OpenAI, Gemini's compatibility layer, a local Ollama, or any compatible endpoint). Image generation and the speech tools are OpenRouter-only and report themselves as not configured elsewhere.                                                                                               |
| `STT_MODEL`             | Voice transcription model (default `google/chirp-3` when unset; gated by the `voice_enabled` toggle).                                                                                                                                                                                                                                                                |
| `TTS_MODEL`             | Speech synthesis model (default `google/gemini-3.8-flash-lite-tts` when unset; gated by the `voice_enabled` toggle).                                                                                                                                                                                                                                                 |
| `TTS_VOICE`             | Synthesis voice (default `Kore`).                                                                                                                                                                                                                                                                                                                                    |
| `TTS_RESPONSE_FORMAT`   | Speech endpoint response format (default `pcm`; raw samples are wrapped in a WAV container server-side). Some models, e.g. OpenAI TTS, also accept `mp3`.                                                                                                                                                                                                            |
| `IMAGE_MODEL`           | Image generation model for the `text_to_image` tool (default `google/gemini-3.1-flash-image` when unset; the tool only runs when the `image_generation_enabled` toggle is on).                                                                                                                                                                                       |
| `VERIFIER_MODEL`        | Reviewer model for the `review_work` tool (defaults to `MODEL`, then the default model, when unset).                                                                                                                                                                                                                                                                 |
| `VERIFIER_MODEL_EFFORT` | Reasoning effort for the reviewer request (default `high` on OpenRouter when unset; on other endpoints unset omits the field, same gate as `THINKING_EFFORT`; an explicit value is always sent).                                                                                                                                                                     |
| `SPEECH_LANGUAGE`       | Spoken language hint for STT (default `en`).                                                                                                                                                                                                                                                                                                                         |

`assets/config.json` holds all runtime settings:

| Key                                 | Purpose                                                                                                                                                                                                                                                                       |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `timeout_ms`                        | Model request timeout.                                                                                                                                                                                                                                                        |
| `retry`                             | Backoff strategy for model requests.                                                                                                                                                                                                                                          |
| `max_command_output_chars`          | Character limit at which tool output is cut to its beginning and end (the system prompt's `{{OUTPUT_LIMIT}}`). The full output always remains available at the `saved_to` path.                                                                                               |
| `tool_call_timeout_s`               | Per-tool-call deadline (default 60). At the deadline the harness asks the call to stop through its cancel event and waits `max_cancel_wait_s` for it to exit; a call that ignores the stop is detached with a hung-call notice. See `parse.py`.                                                                                                |
| `web_search_timeout_s`              | Budget for one web search's engine chain (default 60, not Allegro-scaled). Enforced cooperatively: each navigation's timeout is capped by what remains of the budget, so expiry stops the chain within that navigation.                                                                                                                       |
| `browser_doh_uri`                   | The stealth browser's DNS-over-HTTPS resolver (default `https://dns10.quad9.net/dns-query`, Quad9's unfiltered endpoint; empty disables): encrypted DNS is immune to resolver-level poisoning, and Firefox reads HTTPS DNS records through it, so navigation upgrades to HTTP/3 where the site offers it (bypassing networks that reset TCP TLS by SNI) and ECH configs apply where published. `browser_doh_bootstrap` (default `9.9.9.10`) resolves the resolver's own hostname so a poisoning middlebox cannot redirect the DoH connection. TRR mode 2 falls back to native DNS when the resolver is unreachable. |
| `browser_launch_timeout_s`          | Budget for waiting on a stealth browser's cold start, the search browser and the tool browser alike (default 120, not Allegro-scaled): a cold start is startup, not tool work. The constructor runs in a helper thread, so an expired budget abandons the wait, not the launch; the helper keeps starting the browser for the next call.                                                                                                                  |
| `browser_command_timeout_s`         | Aggregate budget for one `browser` tool command (default 90, not Allegro-scaled): the command runs on the browser's loop raced against the harness's stop event, is cancelled when either limit arrives, and its session is discarded (reset-on-cancel) so the next call starts clean.                                                                                                                           |
| `max_cancel_wait_s`                 | Stop grace after a call's deadline (default 10): how long the harness waits for a stopped worker to exit before detaching from it.                                                                                                                                                                                                            |
| `command_timeout_s`                 | Per-`execute_command` timeout (default 50, kept below `tool_call_timeout_s`). The command is wrapped in `timeout -k 5` inside the pod so hangs are killed inside the pod, before the harness deadline; work longer than one call belongs to the `process` tool, as the system prompt documents.                                                               |
| `pod_doh_uri`                       | The pod's DNS-over-HTTPS resolver (default `https://dns10.quad9.net/dns-query`, Quad9's unfiltered endpoint; empty disables): a bootstrap script runs once on container creation, installs `dnscrypt-proxy`, points `resolv.conf` at it (the pre-DoH resolvers stay as fallbacks), and gives curl transparent HTTP/3 via `/root/.curlrc`, which races QUIC against TCP and falls back automatically for sites without h3. `pod_doh_bootstrap` (default `9.9.9.10`) is pinned inside the resolver stamp so a poisoning middlebox cannot redirect the DoH connection; a custom `pod_doh_uri` needs its provider's own bootstrap IP or an empty `pod_doh_bootstrap` (which makes the script emit a `bootstrap_resolvers` line instead). Any bootstrap failure leaves the pod on native DNS. |
| `pod_doh_bootstrap_alt`             | Secondary bootstrap IP (default `149.112.112.10`) registered as a second resolver entry on the same provider, so dnscrypt-proxy fails over when the primary anycast address is unreachable. Must belong to the provider in `pod_doh_uri`; empty disables the second entry. |
| `net_guard_doh_uri`                 | The browser guard's poison-check resolver (default `https://dns.google/resolve`, JSON DoH over HTTP/1.1; empty uses the system resolver): destination checks resolve through it first so a poisoning resolver cannot tilt the decision, with the system resolver as fallback, which is also the view the browser's TRR mode 2 falls back to. Its job is classification (private vs public vs unresolvable), not IP equality with the browser's own resolver. |
| `pod_memory_mb`                     | Pod memory ceiling in MB (default 8192). Protects the host from runaway processes inside the pod; does not restrict what the model may do inside it.                                                                                                                          |
| `pod_pids_limit`                    | Pod process-count ceiling (default 512), guarding against fork bombs.                                                                                                                                                                                                         |
| `process_max_runtime_s`             | Runtime cap of one detached pod process (default 1200), applied when a spawn omits `max_runtime_s`. Enforced pod-side by a `timeout` wrapper; a capped process exits with code 124.                                                                                            |
| `subagent_max_runtime_s`            | Runtime cap of one subagent turn (default 1800), applied when a spawn omits `max_runtime_s`. Enforced through the session's cancel event; the turn ends as "stopped by the runtime cap".                                                                                       |
| `max_subagent_depth`                | Subagent nesting levels allowed, counting main (default 3: main -> subagent -> sub-subagent; a sub-subagent cannot spawn further).                                                                                                                                            |
| `max_concurrent_subagents`          | How many subagents may run at once (default 4). A spawn beyond the cap is refused; spend protection.                                                                                                                                                                          |
| `wait_for_timeout`                  | Default deadline of one `wait_for` call (default 120). The wait follows its own budget (plus slack), not `tool_call_timeout_s`; on expiry it returns "still running", never an error.                                                                                         |
| `check_tail_chars`                  | How many characters of output `process check`/`subagent check` and `wait_for` tail (default 4000).                                                                                                                                                                            |
| `max_steps_per_turn`                | Model requests allowed per turn (default 100). Before the cap, the model receives a wrap-up warning; past it, the turn ends with one final request reserved for a `respond` status report.                                                                                    |
| `step_warning_remaining`            | Remaining-steps count that triggers the wrap-up warning (default 20).                                                                                                                                                                                                         |
| `context_budget_tokens`             | Estimated-token budget for the whole conversation (default 100000). When exceeded, old tool outputs are elided first and summarization runs only if the budget is still exceeded.                                                                                             |
| `elide_keep_recent_messages`        | History messages kept untouched by output elision (default 12).                                                                                                                                                                                                               |
| `elide_interval_messages`           | History size at which lossless output elision runs on its own, instead of waiting for the token budget (default 30). Keeps prompts small through long runs; elided outputs remain readable in the pod at their `saved_to` paths.                                              |
| `elide_every_requests`              | Requests between batched elision passes once the message interval is reached (default 10). Batching keeps the conversation prefix stable between passes so the provider prompt cache stays warm; the token-budget path always elides immediately.                             |
| `elide_user_message_head_chars`     | How much of an archived large user message stays in history after elision (default 300); the rest is replaced by a pointer to the verbatim archive file.                                                                                                                      |
| `user_message_archive_min_chars`    | User message length at which the message is archived verbatim into the pod and annotated with its path (default 2000).                                                                                                                                                        |
| `user_messages_dest`                | Pod directory holding the archived user messages (default `/user_messages`).                                                                                                                                                                                                  |
| `verify_log_tail_chars`             | How many characters of the serialized conversation log the `review_work` reviewer receives (default 128000).                                                                                                                                                                  |
| `max_history_messages`              | History length that triggers compaction as a safety net for degenerate all-small-message histories; the token budget is the primary trigger.                                                                                                                                  |
| `enable_compaction`                 | Master switch for history compaction.                                                                                                                                                                                                                                         |
| `max_visible_tool_calls`            | UI limit on rendered tool rows.                                                                                                                                                                                                                                               |
| `voice_enabled`                     | Voice category toggle (default `true`): off hides the mic, disables the top-bar voice icon, and makes the speech tools report themselves as unavailable.                                                                                                                      |
| `continuous_conversation`           | Hands-free voice mode (default `false`): in voice mode, the client reopens the microphone after each reply reaches the chat; applied through the settings GUI on Save and Restart.                                                                                            |
| `image_generation_enabled`          | Image generation toggle (default `false`): off makes `text_to_image` report itself as unavailable; on, the model may generate images.                                                                                                                                         |
| `log_file`                          | Log file name (written to the project root).                                                                                                                                                                                                                                  |
| `session_dump_file`                 | Per-session message dump file name template.                                                                                                                                                                                                                                  |
| `server_host`, `server_port`        | Web server binding.                                                                                                                                                                                                                                                           |
| `session_idle_timeout_min`          | Idle web sessions are reaped after this.                                                                                                                                                                                                                                      |
| `upload_dest`                       | Default upload destination inside the pod.                                                                                                                                                                                                                                    |
| `voice.silence_timeout_ms`          | Mic silence that ends a voice recording.                                                                                                                                                                                                                                      |
| `voice.silence_threshold_dbfs`      | Loudness level treated as silence.                                                                                                                                                                                                                                            |
| `voice.max_recording_ms`            | Hard cap on one voice recording.                                                                                                                                                                                                                                              |
| `voice.narration_reveal_timeout_ms` | How long a narrated reply stays hidden while speech is being generated before its text is revealed anyway (default 30000).                                                                                                                                                    |
| `voice.voice_mode_on`               | Initial voice-mode state pushed to the client on every fresh session (page load, new chat, server restart). Accepts booleans and "true"/"false" strings; voice never persists across sessions.                                                                                |

The data-home `config.json` is seeded from the bundled defaults on first run
and is never re-merged with them afterwards. Upgrading the package therefore
does not change any value in an existing install: a changed default (for
example a recalibrated `max_history_messages`) must be applied by hand to
`~/.syna/config.json` (repo root in a checkout).

Static assets are bundled in `src/syna/assets/`: `prompt.md` (system prompt template
with the `{{CURRENT_DATE}}`, `{{COMMAND_TIMEOUT}}` and `{{OUTPUT_LIMIT}}`
placeholders filled at build time; tool documentation is not duplicated
into the prompt; the request's tool schemas carry it), `verifier_prompt.md` (system prompt for the isolated
`review_work` reviewer), `compaction_prompt.md`, `chat.html` and `setup.html`
(web UIs), and `vendor/` (markdown-it, KaTeX, fonts, icons, logo).

### Editing the system prompt or tool docstrings

Prompt and tool-description additions are expensive and near-irreversible:
every token is paid on every request, and text accretes because additions
are easy to justify and nobody dares remove them. Before adding anything,
apply three tests: does the model have to know this? Will it matter over
50% of the time across a multitude of tasks? Can the model learn it
reliably from the tool's own output or trial and error? If a tool's output
already carries the information at the moment it matters, a note, an
error message, a ref, the description does not need it. Overspecific
examples and under-the-hood behavior notes belong here in the technical
description, not in the prompt or docstrings; those carry only the
contract the model must know up front: parameters, units, and
non-obvious constraints that output cannot teach.

### `src/syna/config.py`

Configuration and logging: `load_config()` (reads `assets/config.json` and
merges the bundled defaults under the user's file recursively; user
values win, and keys an older install's file lacks appear with their
default values, so new required keys added in an upgrade no longer crash
direct-indexing callers), `get_logger()` (file logger),
`get_project_root()`, `normalize_base_url()`
and `is_openrouter_base_url()` (endpoint classification),
`sticky_routing_fields()` (per-endpoint-family cache-routing request
fields: `session_id` for OpenRouter, `prompt_cache_key` for OpenAI,
nothing for strict custom endpoints), and loaders for the
protocol limits the prompt and tools consume: `load_output_limit_chars()`
(head-and-tail cut), `load_command_timeout_s()` and
`load_tool_call_timeout_s()` (both subject to the Allegro scale), and the
unscaled web-search budgets `load_web_search_timeout_s()`,
`load_browser_launch_timeout_s()`, and `load_max_cancel_wait_s()`, the
browser's encrypted-DNS settings `load_browser_doh_uri()` and
`load_browser_doh_bootstrap()`, the pod's `load_pod_doh_uri()` and
`load_pod_doh_bootstrap()`, and the background-work budgets
`load_process_max_runtime_s()`, `load_subagent_max_runtime_s()`,
`load_max_subagent_depth()`, `load_max_concurrent_subagents()`,
`load_wait_for_timeout()`, and `load_check_tail_chars()`.

## 4. Architecture

The package is organized into three subpackages layered over a shared
foundation; every cross-package import points downward, so there are no
import cycles.

- Foundation (package root): `config.py`, `paths.py`, `state.py`,
  `interaction.py`, `ask_utils.py`, plus the entry points
  (`__init__.py`, `__main__.py`, `server.py`, `setup.py`).
- `syna.protocol`: conversation wire format: channel sentinels, output
  bounding, context management, compaction, user-message archives,
  tool-call parsing, and schema generation.
- `syna.capabilities`: what the agent can do: the tool registry plus
  browser, web search, editing, vision, imaging, image generation,
  speech, verification tools, and the background-work layer (detached
  pod processes and subagents).
- `syna.conversation`: the agent loop: session orchestration and the
  interactive CLI with its slash commands.

Every module and the chapter that describes it:

| Module                                      | Described in                                           |
| ------------------------------------------- | ------------------------------------------------------ |
| `src/syna/__init__.py`                      | [The agent loop](#srcsyna__init__py)                   |
| `src/syna/ask_utils.py`                     | [The agent loop](#srcsynaask_utilspy)                  |
| `src/syna/capabilities/browser.py`          | [The browser stack](#srcsynacapabilitiesbrowserpy)     |
| `src/syna/capabilities/editor.py`           | [Capabilities](#srcsynacapabilitieseditorpy)           |
| `src/syna/capabilities/image_generation.py` | [Capabilities](#srcsynacapabilitiesimage_generationpy) |
| `src/syna/capabilities/imaging.py`          | [Capabilities](#srcsynacapabilitiesimagingpy)          |
| `src/syna/capabilities/net_guard.py`        | [The browser stack](#srcsynacapabilitiesnet_guardpy)   |
| `src/syna/capabilities/processes.py`        | [Capabilities](#srcsynacapabilitiesprocessespy)        |
| `src/syna/capabilities/subagents.py`        | [Capabilities](#srcsynacapabilitiessubagentspy)        |
| `src/syna/capabilities/speech.py`           | [Voice and narration](#srcsynacapabilitiesspeechpy)    |
| `src/syna/capabilities/tools.py`            | [Capabilities](#srcsynacapabilitiestoolspy)            |
| `src/syna/capabilities/verification.py`     | [Capabilities](#srcsynacapabilitiesverificationpy)     |
| `src/syna/capabilities/vision.py`           | [Capabilities](#srcsynacapabilitiesvisionpy)           |
| `src/syna/capabilities/web_search.py`       | [The browser stack](#srcsynacapabilitiesweb_searchpy)  |
| `src/syna/config.py`                        | [Configuration](#srcsynaconfigpy)                      |
| `src/syna/conversation/ask.py`              | [The agent loop](#srcsynaconversationaskpy)            |
| `src/syna/conversation/commands.py`         | [The agent loop](#srcsynaconversationcommandspy)       |
| `src/syna/conversation/session.py`          | [The agent loop](#srcsynaconversationsessionpy)        |
| `src/syna/interaction.py`                   | [The agent loop](#srcsynainteractionpy)                |
| `src/syna/paths.py`                         | [Running and operations](#srcsynapathspy)              |
| `src/syna/pod_io.py`                        | [Architecture](#srcsynapodiopy)                        |
| `src/syna/protocol/channel.py`              | [The protocol layer](#srcsynaprotocolchannelpy)        |
| `src/syna/protocol/compaction.py`           | [The protocol layer](#srcsynaprotocolcompactionpy)     |
| `src/syna/protocol/context.py`              | [The protocol layer](#srcsynaprotocolcontextpy)        |
| `src/syna/protocol/inspect_module.py`       | [The protocol layer](#srcsynaprotocolinspect_modulepy) |
| `src/syna/protocol/output.py`               | [The protocol layer](#srcsynaprotocoloutputpy)         |
| `src/syna/protocol/parse.py`                | [The protocol layer](#srcsynaprotocolparsepy)          |
| `src/syna/protocol/tool_schemas.py`         | [The protocol layer](#srcsynaprotocoltool_schemaspy)   |
| `src/syna/protocol/user_messages.py`        | [The protocol layer](#srcsynaprotocoluser_messagespy)  |
| `src/syna/server.py`                        | [The web server](#srcsynaserverpy)                     |
| `src/syna/setup.py`                         | [Running and operations](#srcsynasetuppy)              |
| `src/syna/state.py`                         | [Architecture](#srcsynastatepy)                        |
| `webui/`, `scripts/build.py`                | [The web UI](#10-the-web-ui)                           |
| `tests/`, `evals/`, `mock/`                 | [Testing](#12-testing)                                 |
| `website/`                                  | [Website](#13-website)                                 |

### `src/syna/pod_io.py`

Serialized container I/O: one lock around every bollard call.

Bollard's client multiplexes all container calls (exec, copy in either
direction, stop, remove) over one persistent HTTP connection, and
concurrent calls corrupt it (`CannotSendRequest` mid-request, observed
live). `pod_exec`, `pod_copy_to`, `pod_copy_from`, and the generic
`pod_call` wrap every container API call in the codebase; nothing may
call the container object directly. The lock is a leaf in the lock
order: the background registry lock is always taken before it, never
after.

### `src/syna/state.py`

Global Docker client plus per-thread container registry and per-call context.

- `register_container` / `unregister_container` / `get_registered_container`
  bind a container to the calling thread (each web session worker thread binds
  its own).
- `get_container()` returns the thread's container, falling back to a lazily
  created shared one for host-side use (CLI commands).
- `create_container()` runs the pod with `HostConfig` from
  `build_pod_host_config()`: the init process (`Init`) reaps orphaned
  background processes, and `Memory` / `PidsLimit` ceilings (`pod_memory_mb`,
  `pod_pids_limit`) protect the host from runaway work inside the pod
  without restricting what the model may do there. Right after start,
  `pod_init.run_pod_init_steps()` runs the pod's initialization scripts
  and blocks until they finish (one asset script per entry in the step
  registry; each step logs its duration and fails open): the DoH step
  installs `dnscrypt-proxy`, points the pod resolver at Quad9's
  unfiltered endpoint over HTTPS (`pod_doh_uri`, `pod_doh_bootstrap`,
  `pod_doh_bootstrap_alt`, pinned inside dnscrypt stamps), and enables
  transparent HTTP/3 for curl.
- Self-heal: a connection-level failure from `run_container` (dead pipe,
  closed socket; the engine quit or the host slept mid-session) triggers
  one client reset (`_reset_dead_client`, also the body of
  `close_docker_client`) and re-init through bollard's discovery, then a
  single retry; other errors propagate unchanged. The heal covers
  container creation: a session whose engine dies mid-turn keeps failing
  its `execute_command` calls until the next turn that (re)creates a pod;
  the per-call errors are still honest and a fresh session self-heals.
- Per-call context (thread-locals, set inside the tool worker thread by
  `execute_tool_with_timeout`): `set_call_output_path` /
  `get_call_output_path` carry the `/tool_outputs/` path where the current
  call's full output belongs, `set_last_exit_code` / `pop_last_exit_code`
  carry the exit code `execute_command` reports back to the result protocol,
  and `set_cancel_event` / `get_cancel_event` carry the event the harness
  sets when the call passes its deadline (a tool that honors it stops
  promptly and raises `ToolCancelled`).
- `next_call_index()`: monotonically increasing counter used for the
  zero-padded `/tool_outputs/NNNN_tool.txt` file names.
- `init_docker_client` / `close_docker_client` manage the global client
  lifecycle; `reset_shared_container` destroys the shared pod so the next
  `get_container()` lazily starts a fresh one (CLI `/newchat`).
- `ensure_engine_ready()` verifies the engine endpoint end to end by
  constructing a bollard client, whose discovery brings the Podman machine
  up (starting it, initializing it first when it does not exist, and
  restarting a registered machine whose endpoint never came up) whenever
  no pipe/socket is reachable. The wizard's sanity check uses it, so a
  machine that was installed but never initialized fails (and self-heals)
  during setup instead of at the first tool call. Only bollard's wire
  logger (`bollard.wire`) is quieted; operational bollard messages stay
  visible in the syna log.
- `init_docker_client` is serialized with a lock: concurrent sessions wait
  for one in-flight bring-up instead of racing it, and a failed
  constructor rolls the init state back so the next call retries instead
  of finding the half-initialized leftovers forever.
- Session workers survive container-creation failure (`_start_container`
  pushes the error and retries on the user's next message) and push a
  "Starting the container engine..." status while bring-up runs, so the
  window without a container is visible in the UI.

### Concurrency model

- CLI: one thread; tools run in short-lived daemon threads created by
  `parse.py` to enforce each call's deadline: at the deadline the harness
  asks the call to stop and waits for it to exit, so the conversation never
  advances past a call that is still running (only a worker that ignores
  the stop request is detached). The worker thread
  inherits the container binding, the question responder, the
  `review_work` conversation-log provider, and the call's
  output path, and reports the tool-recorded exit code back to the parser.
- Web: each session has one worker thread that owns its container and runs
  turns sequentially; tools inside a turn run in per-call daemon threads that
  inherit the session's container binding and responder. A call that honors
  its stop request ends within the stop grace and delivers its real outcome;
  only a worker that ignores the stop is detached and keeps running until
  the call completes (for container commands the model can terminate them
  with a follow-up `execute_command` since they run as real processes inside
  the pod; the browser tool answers a mid-flight cancellation by discarding
  its shared session instead). A pending `ask_user` question
  blocks both the tool worker and the turn thread indefinitely, by design,
  until the user answers or stop/new-chat/teardown releases the pending
  question with a cancellation sentinel. "Send now" crosses threads in the
  other direction: the WebSocket handler thread moves the item out of the
  inbox into the session's lock-guarded injection list, and the turn loop
  drains it at its next request boundary; the only shared mutable state is
  that list plus the condition-guarded inbox deque.

### Prompt caching and prefix stability

Agentic loops resend the full history on every step, so provider-side
prompt caching is the largest lever on cost and latency. Syna's approach,
in three parts:

1. **Routing keys per endpoint family** (`config.sticky_routing_fields`):
   OpenRouter requests carry a top-level `session_id` (the session's
   client id), which activates provider sticky routing from the first
   request: every request of a conversation lands on the provider
   holding the warm cache. OpenAI endpoints carry `prompt_cache_key`,
   the documented equivalent. Other custom `BASE_URL`s carry neither
   field: strict OpenAI-compatible servers reject unknown top-level
   fields with a 400 (the same failure mode the ungated `reasoning`
   object had), and they still benefit from automatic prefix caching.
2. **Prefix stability**: provider caches match the request prefix byte
   for byte, so the common path is strictly append-only, pinned by
   `test_request_prefix_is_stable_across_steps`. The documented
   invalidation events are the deliberate ones: batched elision passes
   (every `elide_every_requests` requests), compaction, and the
   purge-after-repair splice. The system prompt changes only across
   days (`{{CURRENT_DATE}}`), never between requests of a session.
3. **Measurement**: per-response cached-token counts are logged on every
   request and aggregated into one session-end summary line (requests,
   prompt tokens, cached tokens, hit ratio). A low ratio on a long
   session points at something repeatedly invalidating the prefix; that
   measurement decides whether elision cadence constants ever need
   tuning.

## 5. The protocol layer

The conversation wire format shared by every interface: how
system-originated content is framed so the model cannot forge it, how
tool output is bounded and persisted, how history stays inside its
budget, and how tool calls are parsed, validated, and turned into
schemas.

### `src/syna/protocol/channel.py`

System and agent communication channel separation using Unicode sentinels:
`SENTINEL_OPEN = "\uE000"`, `SENTINEL_CLOSE = "\uE001"`. System-originated
messages (tool results, harness notices, compaction summaries) are wrapped via
`wrap_channel(kind, body)`; kinds in use are `TOOL_RESULTS`, `HARNESS_NOTICE`
and `COMPACTION`. `harness_notice(text)` wraps a message body in the
`<harness_notice>` tags the system prompt describes. Any assistant response
containing reserved Private Use Area sentinels (U+E000 to U+E0FF) raw or as
unicode escapes is intercepted by the sanitization gate before entering
history, defanged, and rejected as channel impersonation. `escape_channel_text`
additionally neutralizes the literal `<tool_results>` / `<result>` /
`<harness_notice>` marker strings inside untrusted tool output before it is
embedded in a result block, so web content cannot forge the framing.

### `src/syna/protocol/output.py`

Cleaning and bounded rendering of tool output shown to the model:
`strip_ansi_escapes`, `collapse_progress_lines` (carriage-return progress
updates reduce to their last segment), `head_and_tail` (keeps the beginning
and end of long output with an explicit omission marker pointing at the
`saved_to` file), and `bounded_output` (cleaning plus cut plus an explicit
`(no output)` placeholder for empty results).

### `src/syna/protocol/context.py`

Token estimation and elision for context management:
`estimate_tokens` / `estimate_history_tokens`: a script-aware heuristic
(about 1.5 characters per token for CJK scripts and fullwidth forms, 4
for everything else; measured against the o200k tokenizer it tracks real
counts within about 6% on Chinese, Japanese, and Korean samples, where a
single global ratio would underestimate CJK-heavy text by roughly 2.5x;
deliberately no tokenizer dependency),
`elide_tool_outputs`: replaces old result
outputs that carry a `saved_to` path with a one-line pointer to the pod
file, leaving the system message, the most recent messages, and anything
unrecoverable untouched, and `elide_archived_user_messages`, which
replaces old archived user messages (marked by `user_messages.py`, and
only when the marker carries the session's archive id) with
a head excerpt of `elide_user_message_head_chars` characters plus a
pointer to the verbatim archive file. Both passes are idempotent.

### `src/syna/protocol/compaction.py`

History-compaction support: loads the compaction prompt, renders history as a
labeled transcript (`[pinned user request]`, `[user]`, `[tool_results]`,
`[harness_notice]`, `[assistant]`, `[previous summary]`), and builds the
summarization request. The first real user message is kept verbatim in history
after compaction and is additionally shown to the summarizer as the
`[pinned user request]` block, so summaries represent the user's request
faithfully instead of inferring the task. Compaction summaries are wrapped
with the `COMPACTION` sentinel channel and `<harness_notice>` tags
(`format_compaction_message`) and detected via `is_summary_message` so
repeated compactions merge rather than duplicate.

### `src/syna/protocol/user_messages.py`

Verbatim read-only archives of large user messages inside the pod. When a
message is at least `user_message_archive_min_chars` long,
`archive_user_message(text, container, seq)` writes it to
`<user_messages_dest>/NNNNNN.txt` (host temp file → `copy_to`, written
as bytes so text-mode newline translation cannot corrupt the verbatim
copy, then `chmod 444`; the model has root, this is an integrity
barrier, not a security boundary) and the history copy is annotated with
the archive path via `USER_MESSAGE_MARKER_TEMPLATE`; the marker carries
a per-session random archive id (generated in `session.py`), and
`ARCHIVE_MARKER_PATTERN` is what `context.py` elides against: a marker
with a foreign id is a quotation, not an archive, and is left alone. Any
failure is logged and returns None so a broken archive never blocks the
turn.

### `src/syna/protocol/parse.py`

Tool-call parsing and execution under native OpenRouter tool calling.

- `build_signature(tool_name, tool_arguments)`: one-line python-like
  rendering of a call.
- `load_tool_call_timeout()`: configured `tool_call_timeout_s` (default 60).
- `_unpack_tool_call(raw_call)`: extracts call ID, tool name, decodes JSON
  arguments string or dict, and reports argument decoding errors. When a
  strict parse fails, a repair pass retires the double-escaped-quote
  serialization some models emit (`\\"` where `\"` belongs, observed with
  GLM Flash): it only fires on an already-failing payload, logs when it
  repairs, and unrepairable errors quote the raw arguments around the
  failure position so self-correction is one-shot.
- `ParseOutcome(structural_error, outputs, warnings, think_signatures)`: returns
  `outputs` carrying one entry per processed call in reply order, including
  skipped calls; entries for calls that never reached dispatch carry `"error": True`
  and `"status": "error"` (or `"skipped"`). Executed entries carry `"call"`
  (position), `"tool_call_id"` (matching the provider ID), `"status"`
  (`ok` / `error` / `timeout` / `skipped`), `"exit_code"`, and `"saved_to"`.
- Sequential execution with stop-on-first-failure: an invalid argument,
  unregistered tool, raised exception, timeout, or non-zero command exit
  marks the rest of the batch `skipped`. Every call receives a result row so
  every `tool_call_id` receives a matching result message. `think` produces
  a `(noted)` tool result and reports its signature via `think_signatures`.
- Browser command failures (`browser` views starting with "ERROR:")
  record exit code 1, so they surface as `error` rows and stop the batch;
  `read_file`/`edit_file` translate Docker 404s into clean "No such file or
  directory" errors with exit code 1, while other Docker errors propagate
  unchanged.
- `execute_tool_with_timeout(tool_name, tool_arguments, timeout_s,
output_path)`: runs the tool in a daemon worker thread, propagating the
  calling thread's registered container, question responder, conversation-log
  provider (`review_work`'s reviewer snapshot), subagent nesting level,
  the call's output path, and
  the call's cancel event into the worker (all thread-locals), and reports
  back the tool-recorded exit code. The worker thread exists to enforce the
  call's deadline, not to let the conversation move on while work continues:
  the model never sees a result while the call is still running. At the
  deadline the harness sets the cancel event and waits up to
  `max_cancel_wait_s` for the worker to exit; a worker that exits within the
  grace delivers its real outcome (a late completion is a result, a failure
  is an error, and a `ToolCancelled` exception becomes the deadline notice:
  "stopped after N seconds, nothing is running in the background"), while a
  worker that ignores the stop request is detached with the hung-call notice
  ("may still be running in the background"), an anomaly, not the normal
  timeout. For `execute_command` the hung notice additionally names the
  command runtime the tool itself enforces (`command_runtime_limit_s`), so a
  hung call is understood as the container call having stopped responding.
  `ask_user` and `review_work` are dispatched with a `None` deadline so a
  pending question never expires, and the reviewer request is bounded by the
  model client's own timeout/retry policy instead.
- `call_deadline_s(tool_name, tool_timeout_s, tool_arguments)`; the per-call
  deadline:
  `tool_call_timeout_s` for regular tools (subject to the thread's Allegro
  scale), `None` for `ask_user`/`review_work`, and for the self-timed tools
  their own worst-case composition: `web_search.search_deadline_s()` (the
  pacing lock's wait cap for any searches queued ahead of it plus the
  searcher's own budget and slack), `browser.browser_deadline_s()` (the
  launch budget plus the command budget and slack), and
  `processes.wait_for_deadline_s(tool_arguments)` (the call's own
  `max_wait_s` or the configured `wait_for_timeout`, capped, plus slack so
  the tool's graceful "still running" return wins over the harness stop),
  bug guards that only
  fire when the tool's internal enforcement has failed, never the routine
  limit.
- `_persist_tool_output(output_text, saved_to, persist_outputs)`: saves
  host-side tool output (web tools, file tools, ...) into the pod at
  `/tool_outputs/` via `copy_to` and chmods the file read-only;
  `execute_command` writes its own file
  pod-side instead. Web sessions persist through the thread's bound
  container; CLI sessions opt in via `persist_outputs` and use the shared
  pod. When no container can be used (unit tests, persistence errors) the
  output is still delivered; only the `saved_to` attribute is dropped.
- Output shown to the model is cleaned (ANSI stripped, progress lines
  collapsed, empty -> `(no output)`) and cut to head-and-tail at
  `max_command_output_chars`.
- `parse_and_execute_tools(tool_calls, on_tool_event, echo_errors,
tool_timeout_s)`: validates arguments against declared signatures; emits
  `started` / `finished` / `error` / `skipped` events with `tool_call_id`,
  `raw` JSON, and parsed `arguments` for front-end call details; `failed=True` is
  passed on finished events for failed runs so the UI can style them.

### `src/syna/protocol/inspect_module.py`

Extracts function names, docstrings and argument metadata via `ast`; used to
inject live tool documentation into the system prompt and to validate tool
call arguments in `parse.py`. Each argument carries its name, type
annotation, whether it has a default, and the default's source text.

### `src/syna/protocol/tool_schemas.py`

Generates OpenRouter/OpenAI-compatible tool definitions from `tools.py` via
`ast` function metadata. Converts Python type annotations to JSON Schema
properties (`str` -> `string`, `int` -> `integer`, `list` -> `array`, `dict` -> `object`),
excludes defaults from `required`, and parses `- `arg` (type, required): ...`
docstring lines into property descriptions while stripping them from the tool
description. `get_tool_definitions()` returns the schemas for all registered
tools. Note: any `- `-prefixed docstring line is treated as an argument
description, so non-argument bullet lists in tool docstrings must use `* `
bullets (see `browser`) or they are pulled out of the tool description.

## 6. The agent loop

The turn lifecycle shared by the CLI and the web UI: `session.py` owns
the loop, `ask_utils.py` sends the model requests, and
`interaction.py` bridges the `ask_user` tool to the host interface.

### `src/syna/conversation/session.py`

`AgentSession`: the shared conversation loop for CLI and web.

- Builds the system prompt from `assets/prompt.md`: fills `{{CURRENT_DATE}}`,
  `{{COMMAND_TIMEOUT}}` and `{{OUTPUT_LIMIT}}`. Tool documentation is not
  injected into the prompt; the request's tool schemas are the single copy.
  With `build_prompt(allegro=True)` the Allegro speed rule is appended
  under its own `## Allegro mode` h2 section, and `{{COMMAND_TIMEOUT}}`
  is reported halved, the value enforcement actually uses.
- `set_allegro(enabled)` applies the Allegro profile and rewrites the
  system message in place, only on an actual change: a no-op leaves the
  provider prompt cache untouched, a real mode change invalidates it
  once. The profile: thinking effort becomes the faster of (user
  setting, `'low'`, OpenRouter only: custom endpoints and faster user
  settings are left alone), `request_delay` halves, `max_steps_per_turn`
  halves with `step_warning_remaining` clamped to half the halved
  budget. Sessions start in normal mode.
- `run_turn` wraps the turn in the thread-local timeout scale
  (`config.set_timeout_scale`): in Allegro, `load_command_timeout_s` and
  `load_tool_call_timeout_s` halve at enforcement time for exactly the
  turn's duration on the worker thread, with no cross-session leakage.
- Loads dynamic tool schemas via `get_tool_definitions()` from
  `tool_schemas.py`, filtered through the constructor's `excluded_tools`
  (the subagent capability hides `ask_user`, the media generators, and
  `review_work` from its workers).
- `run_turn(query)` requests model responses via `ask_question` with native `tools`
  and `tool_choice="auto"`. When the model returns a plain text response without
  tool calls, the content is delivered to the user via `model_message` and the turn
  completes.
- **Group-atomic self-cleaning history**: malformed tool attempts, their
  resulting tool messages, and corrective `HARNESS_NOTICE` warnings remain in
  history during retries (streak counter capped at `MAX_NO_TOOL_RETRIES = 3`).
  The moment a subsequent reply successfully executes a tool call (or finishes),
  all preceding consecutive failed attempts and notices are spliced out of
  `self.messages`, preventing the model from replicating its mistakes without
  leaving dangling tool call IDs.
- **Closed tool-call groups**: providers reject requests containing assistant
  tool_calls without matching results, so `_close_tool_call_group` answers
  any call left hanging by an abnormal exit: user interruption mid-batch,
  an internal batch error, or a stray call beside the final status report,
  with synthetic `skipped` results, and fills in missing call ids in place.
  Tool-call ids absent from a provider reply are synthesized at the source
  (`ask_utils.py`) and in the parser backstop, keeping every pair consistent.
- **Sentinel channel separation**: tool results are appended as individual
  `role: "tool"` messages wrapped with `format_tool_result_content` on the
  `TOOL_RESULTS` channel: containing a `<result>` element with status, exit code,
  and pod path, corrective warnings, file uploads, step-budget warnings and
  interruptions as `HARNESS_NOTICE`, and compaction summaries as `COMPACTION`.
- **Two-stage context management** (`context.py`): once the history reaches
  `elide_interval_messages`, and additionally whenever estimated tokens
  exceed `context_budget_tokens`, old tool outputs (those with `saved_to`)
  outside the recent window are elided to pointers; lossless, since the
  model can re-read them in the pod. Archived large user messages are
  elided the same way (head excerpt plus pointer to the verbatim file).
  To keep the provider prompt cache warm,
  the rewrite is batched: outside the budget-exceeded path it only runs every
  `elide_every_requests` requests, so history stays append-only (and the
  cache prefix intact) between passes. Every output still becomes a pointer
  eventually. Summarization runs only if the message count reaches
  `max_history_messages` or the budget is still exceeded after elision,
  keeping the system prompt, first real user message, the compaction
  summary, and the tail atomic message slice. The cadence rationale lives in
  [Prompt caching and prefix stability](#prompt-caching-and-prefix-stability).
- **Step budget**: each turn allows `max_steps_per_turn` model requests; when
  `step_warning_remaining` steps are left, the model receives a wrap-up
  notice. Past the cap the turn ends with one final model request with
  `tool_choice="none"`, delivering the status report as natural language; an
  empty report ends the turn with the plain exhaustion error. Both notices
  carry a per-turn share_file ledger: the paths actually shared this turn
  (`format_shared_files_ledger`, fed by the session's `_on_tool_event`
  through `shared_file_metadata`), plus, at exhaustion, the instruction to
  only claim a file is shared if it appears in the list, and to only
  report checks and verifications actually run. The ledger exists
  because the `tool_choice="none"` final request makes claiming "shared"
  possible while making the action impossible: in a live run the exhausted
  turn reported its chart as shared although `share_file` had never run.
- **Loop detection**: identical non-`think` calls repeated
  `REPEATED_CALL_LIMIT` (3) times within one turn trigger a one-time
  "change approach" harness notice; the same guard covers identical think
  notes (lone or batched).
- **Session cache accounting**: every response's (prompt, cached) token
  pair accumulates through `ask_question`'s `usage_callback`; on `close()`
  the session logs one aggregate line (requests, prompt tokens, cached
  tokens, hit ratio) as the health check for prompt-cache behavior.
- **Lone-think acceptance**: a reply containing only `think` calls is
  accepted rather than bounced; the reply persists in history, pending
  corrective notices from earlier malformed attempts are purged, the no-tool
  streak resets, and the loop re-asks.
- Dispatched execution errors persist permanently in history (valuable
  debugging information).
- Neutral abort wording: after 3 consecutive failures, the task is ended with
  neutral language (the word "terminated" is completely removed).
- Compacts history when reaching `max_history_messages` (a safety net; the
  token budget is the primary trigger), retaining the system prompt, first
  real user message, the compaction summary, and the trailing atomic tool
  group. The first real user message is also included in the summarizer's
  transcript as a labeled `[pinned user request]` block, so the summary
  reflects the user's actual request; the summary notice states which parts
  were kept verbatim.
- Emits JSON events (`status`, `tool_call`, `model_message`, `compaction`,
  `error`) with quiet corrective statuses and full `raw`/`arguments` details.
  When a tool's raw return value is a dict it also travels as
  `structured_output` on the finished event, so consumers (the web server,
  the Worlds benchmark harvest) get the structured share_file metadata the
  stringified `output` display field cannot carry.
  A `skipped` parser event renders like a finished tool row.
- User interruption raises `TurnCancelled` inside the `started` event hook.
- **Queued-message injection ("Send now")**: `queue_injection(item)` holds
  a queued web message thread-safely; at the top of the turn loop: after
  the previous tool batch's results are in history and before the next
  `ask_question`: `apply_injections()` appends every pending message to
  `self.messages` (through `_prepare_user_message`, so large injected
  messages archive like turn-starting ones) and emits a `user_message`
  event carrying the message's client id. The batch is never broken and
  the turn never interrupted: the model simply cannot make another
  request without the message being in the same one. Injections left
  pending when a turn ends (final response, error, cancellation) are
  returned to the web session's inbox by the worker, so they run as the
  next turn instead of being dropped.
- **User message archives**: before appending the user message, `run_turn`
  hands it to `_prepare_user_message`: messages at least
  `user_message_archive_min_chars` long are archived verbatim into the pod
  by `user_messages.py` and annotated with the archive path (a failed
  archive logs and leaves the message unannotated). `_reviewer_log`
  provides the `review_work` reviewer its conversation snapshot: the
  pending review call is filtered out of the trailing assistant message
  (its sibling calls in the same batch stay; the message disappears only
  when the review call was its sole content), and the provider is
  installed as a thread-local around
  `parse_and_execute_tools` and popped on every exit path.

### `src/syna/ask_utils.py`

Shared helpers: `show_welcome()` banner and `ask_question(client, model,
messages, delay, effort, dump_path, response_format, accept_reply,
session_id, tools, tool_choice, usage_callback)`: sends model requests with configurable delay,
request dumps, and native `tools` and `tool_choice` parameters. Every request
carries `max_tokens` from the `max_output_tokens` config value (default
16384), so reasoning models cannot starve the visible reply of the
provider's default completion budget; `finish_reason` and the
prompt/completion/reasoning/cached token split are logged for every
response. When a `session_id` is given, the request-body routing fields
are chosen per endpoint family by `sticky_routing_fields` (session_id on
OpenRouter, prompt_cache_key on OpenAI, nothing on other custom
endpoints), so provider-side prompt caches stay warm where the endpoint
supports it and strict endpoints never see unknown top-level fields. The full strategy is
described in [Prompt caching and prefix stability](#prompt-caching-and-prefix-stability). An
optional `usage_callback` receives the (prompt_tokens, cached_tokens)
pair of every response, including retried attempts, for session-level
accounting. Accepts an optional `accept_reply`
validator that raises `ReplyRejected` on channel impersonation in content or
tool call arguments. Transient empty responses are retried up to
`EMPTY_RESPONSE_MAX_ATTEMPTS` times.

### `src/syna/interaction.py`

User-interaction bridge between the `ask_user` tool and the host interfaces.
Tools never touch sessions or terminals directly: before running a turn, the
interface registers a responder thread-locally (propagated into tool worker
threads by `parse.py` like the container binding). `ask()` blocks until the
responder returns the user's answer, raising `InteractionError` when no
responder exists (non-interactive runs) and `InteractionCancelled` when the
task was stopped. `PendingQuestion` is the thread-safe state for one pending
web question: the session worker opens it and blocks on the answer queue
while WebSocket-handler threads resolve or release it (a released question
unblocks with a `None` sentinel).

### `src/syna/conversation/ask.py`

CLI loop. Renders session events (`_print_event`), dispatches slash commands
via `commands.parse_and_execute_command`, and feeds plain queries to
`AgentSession.run_turn`. The renderer shows tool calls as they start (in the
progress line), marks failed and skipped rows, and prints a `/get-from` hint
when a `share_file` result carries delivery metadata. Ctrl+C during a turn
cancels the turn instead of exiting (a second Ctrl+C quits); `/newchat`
closes the session and destroys the shared pod. The terminal responder for
`ask_user` prints the question with numbered choices and accepts a number or
free text; Ctrl+C or a closed stdin cancels the pending question and stops
the task.

### `src/syna/conversation/commands.py`

User slash commands (`/send`, `/receive`, `/help`, `/bye`) executed locally on
the host. `send`/`receive` copy files between host and pod through the
thread's container; they open native file dialogs via tkinter, so the CLI's
file-transfer commands require a desktop session. `parse_and_execute_command`
validates and dispatches.

### `src/syna/__init__.py`

CLI entry point. `main()` starts the ask loop, handles Ctrl+C, and closes the
global Docker client on exit.

## 7. Capabilities

The tool families the model can invoke, one section per family. The
registry, the pod-artifact conventions, and the `execute_command`
hardening live in `tools.py`. The browser stack has its own chapter
([The browser stack](#8-the-browser-stack)); speech is covered in
[Voice and narration](#11-voice-and-narration).

### `src/syna/capabilities/tools.py`

The tools exposed to the model: `think` (no-op reasoning record; produces a
`(noted)` tool result), `execute_command` (shell or argv style inside the container),
`read_file`, `edit_file` (both proxied into the pod through temp copies),
`web_search`, `browser`, `describe_image`, `speech_to_text` (pod audio →
transcript, saved to /workspace/transcripts with a preview in the result;
optional ISO-639-1 `language` hint, auto-detect when omitted),
`text_to_speech` (pod text → speech audio in /workspace/audio),
`text_to_image` (prompt → generated image in /workspace/images via
`image_generation.py`; both speech tools reuse `speech.py`),
`share_file` (registers a pod
path for user download; a missing path is reported as a normal error row
with exit code 1, and the delivery metadata reaches the client through the
`structured_output` event field), `ask_user` (asks the user a question
with optional choices and blocks until they answer; validates its arguments,
returns a clean error dict in non-interactive contexts, and propagates
`InteractionCancelled` when the task is stopped mid-question; the client
adds the "Other: Type your own response" option itself, and an empty
choices array means an open text answer), and `review_work` (isolated
adversarial verification through `verification.py`; the reviewer's report
is the tool result). The final answer to the user is
delivered as natural language text when the model completes its work without
tool calls.

Pod artifacts are written read-only as an integrity barrier (the model has
root and can undo it; the point is that a result already reported cannot be
silently overwritten; regeneration produces new, timestamped names):
`_write_into_pod` chmods 444 every artifact it copies in (screenshots,
transcripts, audio, generated images), `execute_command`'s pod-side runner
chmods its `/tool_outputs/` capture file, and `_persist_tool_output` in
`parse.py` chmods the host-persisted outputs. Host-side logs and session
dumps are deliberately not read-only: their writers rewrite them in place
(os.replace over a read-only target fails on Windows).

Blocking-operation boundedness (audited when the harness moved to
terminal, cancel-based timeouts; updated when the browser tool got the
same treatment): every network and model request carries an explicit
timeout; the speech and image-generation endpoints use
`httpx.post(timeout=timeout_ms)` and the vision and reviewer requests go
through the model client's configured timeout and retry policy, so those
calls honor the harness deadline or finish just past it, within the stop
grace. The browser's Playwright operations each carry their own timeouts
(navigation 30 s, clicks 4-7 s, selects 3 s, reload 20 s), and their
aggregate is now bounded too: each browser tool command runs under
`browser_command_timeout_s`, raced against the harness cancel event, so
a wedged command is cancelled mid-flight and reported as a timeout
instead of leaning on the harness deadline. The bollard `exec` and
`copy_to`/`copy_from` calls behind `execute_command`, the file tools, and
`share_file` carry no client-side timeout parameter; `execute_command`
bounds itself with the pod-side `timeout -k` kill, and the rest are
bounded by the harness deadline: a wedged engine surfaces as a hung-call
notice after the stop grace instead of blocking the session forever.

Every docstring becomes the model-facing tool documentation through the
request's tool schemas (`get_tool_definitions` in `tool_schemas.py`
splits it into the tool description and per-argument descriptions), so
the docstrings must stay in sync with actual behavior.

`execute_command` hardening (reliability inside the pod, not restriction:
the model keeps full root and can cd, edit system files, and install
packages): the command runs via `timeout -k 5 <command_timeout_s> bash -c`
inside the pod so hangs are killed inside the pod, before the harness
deadline; its output is
redirected to `/tool_outputs/NNNN_execute_command.txt` (file capture means
background processes started by the command cannot hold the exec stream open
and hang the harness); a per-command environment adds non-interactive
conveniences (`DEBIAN_FRONTEND=noninteractive`, pagers to `cat`,
`GIT_TERMINAL_PROMPT=0`, ...) that the model can override inline. A second
exec reads a bounded view of the output file (head-and-tail at
`max_command_output_chars` bytes) together with the recorded exit code;
exit codes 124/137 (timeout killed) map to status `timeout`, other non-zero
codes to status `error`. A timeout-killed call additionally carries an
explicit `TIMEOUT:` notice on the result text stating exactly what happened
(killed at the limit, with the exit code; 137 hedged between the timeout's
final kill and a system kill such as out-of-memory), naming the effective
maximum runtime of one execute_command call (`command_runtime_limit_s()`),
and pointing at background jobs (`nohup ... &`) for longer work. When no call
output path is set (direct host-side
use), it falls back to a plain `container.exec`.

### `src/syna/capabilities/editor.py`

Line-based reading and editing for the `read_file` and `edit_file` tools,
designed to round-trip files byte-exactly. Reads decode with
`surrogateescape`, so bytes that are not valid UTF-8 survive an
unrelated edit untouched (the numbered display maps them to U+FFFD;
`errors="replace"` would have corrupted them permanently on write).
Content splits on `\n` only, matching `grep -n` line numbering, where
`splitlines()` also broke on form feeds and U+2028 and sent edits to
the wrong line, and a file's newline style (CRLF stays CRLF) and
missing trailing newline are preserved on write. Edit operations are
validated before the file is touched: non-dict or multi-key
operations, non-object payloads, missing required keys, unknown operation
types, out-of-bounds line numbers, invalid ranges, overlapping edit
ranges, and a `replace_string` whose `old_string` matches zero or
several times all raise clean `ValueError`s instead of leaking internal
`TypeError`s. `replace_string` swaps the one exact occurrence of a text
snippet (the safer anchor for small edits in files that may have
changed) and runs after the batch's line operations. A successful edit
returns a unified diff of the applied change, bounded to a head-and-tail
view at `DIFF_MAX_CHARS` (2000), empty when the file is unchanged, so the
model can confirm an edit landed without spending a follow-up `read_file`
(a live session showed exactly that wasteful re-read pattern).

### `src/syna/capabilities/imaging.py`

Image processing shared by the vision pipeline and the web server's inline
image endpoint. `fit_image_to_box` writes a PNG copy of a source image
downscaled into a 1024x1024 box (CMYK converted to RGB first);
`image_exceeds_box` tests an open Pillow image against the box;
`MAX_SERVE_BYTES` (50 MB) and `RECOMPRESS_BYTES` (5 MB) bound what the
image endpoint will serve.

### `src/syna/capabilities/vision.py`

`describe_image(file_path, question)`: image understanding through a
dedicated single-turn vision request. Copies the image from the pod,
converts it to PNG downscaled into a 1024x1024 box (via `imaging.py`),
logs the payload size (pixels and PNG bytes, so a failing vision request
leaves data instead of a mystery), base64-encodes it into a fresh
single-message conversation (plain-text response, no JSON mode), and
replaces the bytes with a text placeholder carrying the description so
images never linger in history. The request runs at `medium` reasoning
effort on OpenRouter: without it the provider default there is maximum,
which burns the completion budget on hidden reasoning and can return no
content at all. Other endpoints omit the field (same gate as
`THINKING_EFFORT`), since the OpenRouter-style `reasoning` object is
forwarded verbatim to custom `BASE_URL`s and strict endpoints reject it.

### `src/syna/capabilities/image_generation.py`

Text-to-image generation through OpenRouter's dedicated Image API
(`POST /api/v1/images`). `ImageSettings` (frozen dataclass) carries the
model from `.env` (`IMAGE_MODEL`); when unset the caller reports the feature
as not configured by the user. `generate_image(prompt, settings)` posts the
prompt, decodes `data[0].b64_json`, and returns a `GeneratedImage` (bytes
plus media type; `image/png` when the provider omits it, `extension` maps
the media type to a file extension). The API bills all-or-nothing, so
failures (non-200, malformed response) raise `ImageGenerationError` with a
user-safe message carrying the provider detail.

### `src/syna/capabilities/verification.py`

The `review_work` reviewer session. `run_verification(summary)` runs an
isolated single-turn request: fresh client (`create_model_client`),
`assets/verifier_prompt.md` as the system prompt, **no tools**; whose
user message carries the last `verify_log_tail_chars` characters of the
agent's serialized message history (`json.dumps`, minus the assistant
message holding the pending `review_work` call itself) inside a
` ```text ` block labelled "Last N chars of agent execution log",
followed by the summary the agent intends to report. The reviewer model
is `VERIFIER_MODEL` (falling back to `MODEL`) at `VERIFIER_MODEL_EFFORT`
(default `high`); the reviewer's text is the tool result and nothing
else: no session id, no diagnostic dump, no UI/history artifacts beyond
the tool call and its result. The conversation snapshot arrives through
a thread-local provider (`set_conversation_log_provider` /
`current_conversation_log_provider`), propagated into tool worker
threads by `parse.py` like the container binding; failures follow the
`describe_image` convention: an `{'error': ...}` dict, never a raise.

### `src/syna/capabilities/processes.py`

Detached background processes in the pod, behind the `process` tool
(actions `spawn`, `check`, `kill`). Also the home of the per-session
background registry shared with `subagents.py` (which imports it; this
module never imports session machinery, so there is no cycle).

- **Registry.** `PodRegistry` holds process and subagent records behind
  one lock, the id-reservation set shared by both kinds, and two
  optional hooks the server registers: `event_sink` (subagent UI
  streaming) and `completion_sink` (wake notices). `registry_for(container)`
  keys by container identity, so the session worker, subagent runners,
  and server request threads all resolve the same registry;
  `drop_registry` runs at session teardown. Registries are in memory:
  a server restart loses them (the processes keep running under their
  `timeout` wrapper; `ps` in the pod is the recovery path). All pod
  calls in the whole codebase go through the shared helpers in `pod_io`
  (`pod_exec`, `pod_copy_to`, `pod_copy_from`, `pod_call`): bollard's
  client multiplexes every container call over one persistent HTTP
  connection, and concurrent calls from the watcher, tool workers, and
  server threads corrupt it (observed live as `CannotSendRequest`,
  which once killed a watcher mid-session).
- **Ids.** A `name` argument becomes the id verbatim
  (letters, digits, underscores, and hyphens, with single dots between
  segments; no leading, trailing, or doubled dots, so a name can never
  escape or alias the artifact directory layout); auto ids count per
  kind (`process_0001`). Ids are unique across both registries and never
  released, so `wait_for` ids are unambiguous and output directories
  cannot be aliased. Rejections follow the what/why/how style. All
  model-facing text (tool arguments, docstrings, error strings, notices)
  calls the id the "process name" or "subagent name" — a user-chosen
  `name` becomes the id verbatim, so the two always coincide; the word
  "id" only appears in internal code, logs, and this document.
- **spawn.** One exec starts `setsid bash -c <launcher>`: the launcher
  is a session leader detached from the exec call, backgrounds
  `timeout -k 10 {cap}` (no `setsid` around it, so
  `$!` and `wait` still target timeout itself), records timeout's pid
  into the process's `pgid` file, and records timeout's exit status as
  the process's `exit_code`. Verified live: GNU timeout makes itself
  the process group leader (`setpgid(0,0)`) with the command and its
  children in that group, so timeout's pid is the group id and
  `kill -- -PGID` reaches the whole tree, timeout included. The
  command runs as `exec stdbuf -oL -eL bash -c <command>` (line
  buffering: redirected output is block-buffered otherwise) with
  output going to `/workspace/processes/{id}/stdout` and `stderr`.
  Spawn polls briefly for the `pgid` file and fails cleanly when it
  never appears. Exit code 124 means the runtime cap fired; a manual
  kill of the group reports 128+signal through the same file. No
  host-side reaper: processes die with the pod. A spawn's
  `max_runtime_s` argument overrides the configured cap for that one
  process; the chosen cap is stored on the record, so every report
  (check, wait_for, completion notice) quotes the cap that actually
  wraps the command.
- **check/kill.** `check_process_state` probes state and tails
  (bytes-capped pod-side, base64-framed so the output cannot fool the
  marker parse, truncated to `check_tail_chars` host-side).
  `kill_process` signals the recorded process group (`TERM`, then a
  pod-side SIGKILL follow-up), never touching artifacts; a kill by the
  model marks its completion notice as already-known (it knows), a
  kill by the user leaves it to fire ("stopped by the user").
- **Completion watcher.** One thread per registry, started lazily on
  the first spawn and living for the registry's lifetime (a cheap
  3s poll loop; it only execs while processes exist, and exits when
  the registry is dropped), reads the pod for
  `exit_code` markers and fires the single
  completion notice per process through `fire_completion`: a payload
  with the id, a one-line UI summary, and the model-facing notice. The
  server pushes the summary as a `background_done` event and enqueues
  the notice into the parent worker's inbox, which wakes an idle
  worker or queues behind an active turn — unless `wait_for` already
  returned the final report: it sets the record's
  `collected_by_wait_for` flag, finalize skips the notice for it, the
  server skips enqueueing it, and a notice already queued when
  `wait_for` collected the outcome is dropped at dequeue time
  (`_notice_is_stale`), so work that ends on its own reaches the model
  exactly once. Collection via `check` never sets the flag: a check
  racing the real end may legitimately produce two messages. All pod
  calls serialize over
  bollard's single HTTP connection, so a notice can lag behind a
  long-running `execute_command` on the same pod; a late notice is
  still a correct one. A failed probe is retried: only consecutive
  failures declare the pod gone, so one transient error under load
  cannot silence notices for the rest of the session (observed live
  before the retry existed).

### `src/syna/capabilities/subagents.py`

Background subagents: full `AgentSession` instances on their own
threads, behind the `subagent` tool (same action shape). Same
system prompt, model configuration, and shared pod; `AgentSession` is
imported lazily inside the runner (importing it at module level would
close a cycle: tools -> here -> session -> parse -> tools).

- **spawn.** Gating before anything expensive: depth first (the
  spawning context's nesting level comes from a thread-local set by
  the runner and propagated by `parse.py` onto tool worker threads;
  a spawn is allowed while child level <= `max_subagent_depth`), then
  the `max_concurrent_subagents` cap, then id reservation. The first
  user message is a nesting header ("nesting level N of M; you cannot
  ask the user questions") followed by the goal. Tools a background
  worker cannot use are removed from its schemas
  (`AgentSession(excluded_tools=...)` seam): `ask_user`,
  `text_to_speech`, `text_to_image`, `review_work` (its work is judged
  by the spawning agent, and the verifier's conversation provider does
  not exist on its threads anyway), and `share_file` (what reaches the
  user is the spawning agent's call, based on the subagent's report).
- **Runtime cap.** A `threading.Timer` at the record's runtime cap
  (the spawn's `max_runtime_s`, else `subagent_max_runtime_s`) calls
  `session.cancel()`; the turn unwinds at the next pause and is
  recorded as "stopped by the runtime cap", and the same cap value is
  quoted by every report. Allegro inheritance: a
  subagent spawned from an Allegro turn gets `set_allegro(True)` (the
  prompt suffix and the lighter runtime profile), so delegated speed
  matches the spawning turn's.
- **Streaming.** Session events are compacted (`model` text,
  `tool` rows with signature and capped previews, `error`s,
  `share_file` metadata, and a final `done` row), appended to the
  record's capped in-memory history under a lock, and delivered
  through the registry's `event_sink`; the server pushes them as
  non-persistent `subagent_message` events (the subagent viewer
  fetches recorded history from the registry on open, so reconnect
  replays never flood the main event log). A subagent's `share_file`
  call reaches the parent session's sharing machinery, so files it
  shares are downloadable as usual.
- **Artifacts.** The real-time message dump is the session dump
  machinery itself (`session_id="subagent-{id}"` ->
  `message-session-log-subagent-{id}.json`). At check, kill, and
  natural termination the dump is copied into the pod at
  `/workspace/subagents/{id}/messages.json`, with the final response
  alongside as `response.md`; all copies are read-only and a kill
  never destroys them, so every path reported to the model is true
  when it reaches it (the copy always precedes the report; the
  check-path copy is deliberately unconditional so the model always
  reads the latest snapshot). Kill and the runner's finalize pass
  serialize on the registry lock, so a kill report cannot interleave
  with a completion. A kill is a graceful cancel (stop at the next
  pause); its report names the paths immediately, and the completion
  notice confirms the final artifacts afterwards.
- **Completions.** The runner finalizes on every end (natural, cap,
  kill, error): response fallback is the last assistant message in
  history (a cancelled turn emits no final message), then the same
  single completion notice fires as for processes. A kill by the model
  suppresses the notice; a kill by the user delivers "was stopped by
  the user".

## 8. The browser stack

The stealth browser (Camoufox) behind `web_search` and the `browser`
tool: page viewing and interaction, the host network guard, and search
pacing.

### `src/syna/capabilities/browser.py`

Persistent stealth browser session (Camoufox) exposed as LLM-callable actions.
Renders pages through an accessibility-style walker that emits a compact
line-based view (URL, title, scroll position and numbered interactables), and
dispatches commands (`visit`, `click`, `type`, ...) by numeric element ref.
`screenshot_bytes()` captures the current viewport as PNG
(`Browser.screenshot` is its sync wrapper); `tools.py` copies the bytes into
`/workspace/screenshots/` in the pod (unique per-call filenames) so
`describe_image` and `share_file` can use them. Browser-side screenshot
failures follow the same 'ERROR:' result convention as the other commands;
pod-side copy failures propagate.

Hardening: hosts whose ad/identity scripts fail under uBlock (which makes
Fandom wipe the page with an error-report modal) are fulfilled with empty
JavaScript (`NEUTRALIZED_SCRIPT_PATTERNS`); popups are only adopted as the
current page right after a user-initiated interaction, unsolicited ones are
closed; renderer crashes are logged and the page transparently reloaded
(`_recover_from_crash`); `read` retries once and returns a diagnostic instead
of an empty string when the document is blank; page errors, JS errors and
failed requests are logged to the dev log; and every tool command's duration
is logged as it finishes through the shared `_tool` dispatch, so a wedged
navigation (one live incident: a host-level TLS outage left `visit` hanging
until the call's deadline) is visible in the log instead of surfacing only as a
bare tool-call timeout.

Budgets and cancellation (the same terminal-timeout treatment as
`web_search`): the tool browser and the search browser are both
`SharedBrowser` instances: the cold Camoufox start runs in a helper thread
bounded by `browser_launch_timeout_s`, so a caller that outlives the budget
abandons the wait, not the start, and the next call finds the browser warm;
concurrent first callers join one construction, a failed constructor is
raised to every waiter with a fresh attempt on the next call, and
`invalidate` drops the instance with a best-effort teardown in its own
helper thread (a session that wedged mid-command cannot be trusted to close
promptly, and no caller may block on it). Each session command, and the
screenshot capture, runs under `browser_command_timeout_s` (default 90,
not Allegro-scaled; a visit's worst legitimate path is a 30 s navigation
plus challenge waiting), raced against the harness's cancel event inside
the browser's own loop (`Browser._bounded`): the event is read in the
calling thread, where the per-call event is thread-local, and whichever
limit arrives first cancels the Playwright task. A command that lands its
result while the stop is being delivered still counts; the result is
returned; the harness stop is answered with `ToolCancelled` (rendered as
the "stopped, nothing is running" notice) and budget expiry with
`BrowserCommandTimeout`; a command that ignores its cancellation for the
acknowledgement window (five seconds, capped at half the harness's stop
grace so a clean ending always lands before the harness would detach) is
abandoned running on the loop, with a done-callback retrieving its
eventual exception so asyncio never logs unretrieved-task noise. Both
cancellation endings
discard the shared session (reset-on-cancel: a command cancelled mid-flight
leaves the page in an unknown state a later command could land on), so the
next browser call starts a clean browser through the launch budget. The
harness deadline for the tool call (`browser_deadline_s`, consumed by
`call_deadline_s` in `parse.py`) is launch budget + command budget + slack;
a bug guard that only fires when the internal enforcement has failed.

Network guard: the browser runs on the host, so all its traffic is
host-originated. A single `context.route("**/*")` handler (`_guard_route`)
is the choke point for every request: ad/identity neutralization and the
`net_guard` policy (public internet only) in one place, independent of
Playwright's route-matching order. WebSocket handshakes cannot be blocked
by routing, so `_on_websocket` kills sockets to non-public destinations the
moment they appear (the Syna server's Origin check is the second layer).
`tool_visit` additionally validates the destination before navigating with a
model-readable error. The visit pipeline absorbs the behaviors of the retired
DrissionPage fetcher (`web_to_markdown.py`, deleted): after navigation it
waits out anti-bot interstitials (`_wait_past_challenge`, `CHALLENGE_MARKERS`;
returning the view even if one persists, so the model can decide), dismisses
blocking overlays (`_dismiss_blocking_overlays`, `OVERLAY_DISMISS_JS`), scrolls
gradually to trigger lazy-loading (`_human_scroll`), and waits until the
visible text stabilizes (`_wait_dom_stable`). Overlay discovery is one shared
in-page function (`_OVERLAY_ROOTS_FN` / `__synaOverlayRoots`, used by the
snapshot walker, `read`'s warning, dismissal, and the click fallback's
containment guard) with two independent nets so no single site pattern is
load-bearing: the semantic net catches native dialogs, Bootstrap-style
modals and `role=dialog` surfaces, walking open shadow roots and same-origin
iframes (consent managers like Usercentrics and justETF's transparent
country warning render inside shadow roots, where `querySelectorAll` cannot
reach); the veil net catches hand-rolled walls whatever their markup, a
visible `position:fixed` element covering ≥90% of the viewport, stacking
above the page (`z-index ≥ 10`), carrying an actionable control, and not
containing the page's `main` (so SPA app shells are not mistaken for walls).
Native `<dialog>` counts as blocking only when `:modal` matches; non-modal
`dialog[open]` panels leave the page interactive and stay uncollapsed.
Bootstrap 3's `.modal.fade.in` naming shows why the layered nets matter:
no class rule would catch it, and the role/veil rules do. All discovery is
read-only observation (`elementFromPoint`, `getComputedStyle`); the only DOM
interaction remains the pre-existing consent-button click. While a blocking
overlay is open, the snapshot collapses to just the overlay's subtree so the
model's next action is dismissing it instead of clicking dead refs
(`snapshot(full=true)` opts out; non-blocking banners keep the full page
with a note line; `read` prepends a warning). Dismissal clicks only
consent-language buttons and pure-dismiss controls (`[data-bs-dismiss]`,
`.btn-close`, Close/Cancel), never Save/OK/submit, which could confirm
something on the user's behalf, and `_click_element` skips auto-dismissal
entirely when the clicked element itself sits inside an overlay, so a model
working granularly inside a consent banner is never overridden to
"Accept all". Clicks self-heal: on Playwright's actionability timeout the
overlays are dismissed and the trusted click retried, then a coordinate
click (after re-centering the scroll so fixed headers cannot cover the
target) and a label/JS click (`FALLBACK_CLICK_JS`) are tried; each fallback
is reported in a note, and a final failure carries the actionability
reason: `describe_pw_error` keeps the first line plus keyword-picked,
deduplicated call-log verdicts ("element is not visible", "<label>
intercepts pointer events"), since the log repeats its boilerplate across
retry cycles and a naive line budget drops the verdict entirely. Consent
managers whose buttons live inside cross-origin iframes (Sourcepoint,
Didomi, Quantcast) are invisible to the in-page walker, so when an overlay
blocks the view the harness scans child frames over Playwright's frame API
(`_scan_frames`, `FRAME_ELEMENTS_JS`) and allocates frame-scoped refs from
a separate range (`FRAME_REF_BASE`), resolved through a Python-side
registry and cleared on navigation; the click chain works on them
unchanged. Tables render as data on both
paths: the compact view's walker suppresses flushes inside `<td>`/`<th>`
(`inCell`) so every `<tr>` emits one line with `|`-separated cells no
matter what block elements the cells contain, and `read` extracts text with
`TABLE_AWARE_TEXT_JS`, an innerText-like walk with the same row convention
(nested tables flatten into their cell); the extraction never mutates the
DOM. WebRTC is disabled via `firefox_user_prefs` (it
bypasses HTTP-layer interception and is a standard IP-leak/fingerprint
surface). Note: interception in this Camoufox build pins the browser to
HTTP/2; HTTP/3 is unavailable in both route and proxy modes (verified
against cloudflare-quic.com); absence of H3 is a common real-world pattern.

### `src/syna/capabilities/net_guard.py`

The browser's network policy: public internet targets only. Blocked ranges
(loopback, RFC1918, link-local incl. the cloud metadata service, CGNAT,
ULA, multicast, reserved, documentation) are an explicit list, independent
of `ipaddress.is_global` version drift. `check_destination` parses IP
literals with the WHATWG URL IPv4 semantics browsers actually apply
(decimal, hex, octal, mixed, trailing dot, shortened dotted forms where
the last label carries the remaining bits, e.g. "127.65535" is
127.0.255.255), and resolves hostnames at decision time
through the guard's own DoH endpoint (`net_guard_doh_uri`, default
dns.google's JSON API over HTTP/1.1) so a poisoning resolver cannot tilt
the decision, with the system resolver as fallback (the view the
browser's TRR mode 2 falls back to as well) and a short-lived cache,
blocking names that resolve to any private
address, which closes casual DNS rebinding; a fast-flip rebinding
adversary can still race the browser's own second resolution, which is
why the Syna server independently validates the Host header, Origin, and
token. Resolution failures pass (the
connection fails anyway). `check_url` guards http/https/ws/wss and lets
scheme-less payloads (data:, about:) through. Config:
`browser_allow_private_networks` (default false) and
`browser_allowed_hosts` (exact hostnames, e.g. a local dev server).

### `src/syna/capabilities/web_search.py`

`search(query, delay_s=2)`: searches the query through the stealth browser
(Camoufox) against server-rendered endpoints, trying engines in order
(DuckDuckGo html, DuckDuckGo lite, Bing, Mojeek) until one returns results,
with per-engine redirect unwrapping (DuckDuckGo's `uddg=` wrapper, Bing's
base64 `/ck/a` redirect) and an AD-loaded warm browser kept for the process
lifetime. URLs are not queries: a URL-shaped `query` returns a one-line hint
pointing at the browser tool's `visit` command. A previous ddgs-based
implementation was dropped: its plain-HTTP
engine fleet was blocked or challenged (google 403, brave 429, DuckDuckGo
202 anomaly pages, startpage bot walls), its only working engine (yahoo)
failed 60% of the time, and every failed attempt was bot-shaped traffic
that risked the IP's reputation, which would also poison the stealth
browser. Navigations carry a same-site referer, and an engine that serves
a captcha/challenge page (detected by widget selectors or marker phrases)
is paused for a randomized 15-30 minute cooldown instead of being
re-requested on the next search; repeated visits to a host that demands
verification are the repeat-offense pattern that hardens soft challenges
into IP bans. The search is self-timed and never detached: it blocks until
it completes, fails, or runs out of one of its own budgets. Starting the
stealth browser from cold gets `browser_launch_timeout_s` (default 120, not
Allegro-scaled; a cold start is startup, not search work, and its duration
varies with OS cache state; one live incident had a first-of-day cold start
take 31.5 s and lose its results to a 30 s detach). The constructor runs in
a helper thread so the budget bounds the wait rather than the process: a
caller that outlives the budget raises cleanly while the helper keeps
starting the browser, and when it lands the next call finds it warm; the
in-flight attempt is shared state, so concurrent callers wait on the same
construction instead of racing a second one, and a failed constructor is
raised to every waiter with a fresh attempt on the next call. The engine
chain gets `web_search_timeout_s` (default 60,
not Allegro-scaled), enforced cooperatively: each navigation's Playwright
timeout is capped by what remains of the budget (`min(30 s, remaining)`),
so expiry stops the chain within that navigation, and the pacing and
inter-engine sleeps end early through the harness cancel event. The random
pacing sleep in `[delay_s, 1.5*delay_s)` before every request (fixed waits
are a fingerprint of their own) and the request share a process-wide lock,
so searches from concurrent sessions serialize and stay at least `delay_s`
apart instead of firing together: a queued search waits for the running
one, which always finishes within its budgets, instead of overlapping or
failing fast. The lock's wait cap (launch budget + search budget + slack +
stop grace) outlasts every legitimate hold, so exceeding it means the
running search wedged past every budget, and the waiter fails with a clear
"appears stuck" error. The harness deadline for the tool call
(`search_deadline_s` in `web_search.py`, consumed by `call_deadline_s` in
`parse.py`) is composed to outlast all of it: the lock's wait cap plus
the searcher's own budget and slack, because a queued search spends its
deadline waiting for the lock before its own budgets even start; it is a
bug guard that only fires when the internal enforcement has failed. Every
engine attempt, result count, lock wait, and
the cold-start duration are logged, because a silent chain is undiagnosable
after the fact. Page visiting and conversion
live in `browser.py` (`visit`/`read`): there is exactly one website viewer,
so the model can move from viewing a page to acting on it in the next tool
call.

## 9. The web server

The FastAPI application: per-client session records, the event log and
WebSocket replay, uploads, the speech and image endpoints, and the
hardened local-only boundaries.

### `src/syna/server.py`

FastAPI app. One `SessionRecord` per client id owns an `AgentSession`, a
dedicated container (registered thread-locally in the record's worker thread),
event queues, an event log for WebSocket replay, an `ask_user` pending
question, and shared-file downloads (when a finished `share_file` event
carries `structured_output` metadata, matched via
`session.shared_file_metadata()`; the record copies the pod path to the host
and pushes a `file` event the client renders as a download; worker teardown
releases the container and model client; `share_file` recreates the session
temp dir if it was removed externally). The session inbox is a
`collections.deque` under a `threading.Condition` rather than a `queue.Queue`,
so "Send now" can remove one specific queued message by its client-generated
id: the item is taken out of the queue and, while the worker is busy, handed
to the session as a pending injection (delivered at the turn's next request
boundary); when idle it simply runs as the next turn. If the container engine
is down, the dequeued message goes back to the head of the queue and bring-up
retries after a pause instead of the message being dropped. Messages typed while a
pending `ask_user` question awaits are answers, not queue items. During a
turn the record registers an
`ask_user` responder that pushes nothing extra (the client renders the
question from the tool call's `started` arguments) and blocks on the pending
answer queue; the WebSocket handler resolves the question on an `answer`
message or treats a plain `message` as the typed answer instead of queueing a
turn (the message's Allegro flag and client id are captured at enqueue time in
`_inbox_item_from_message`; a `queued: true` `user_message` event docks the
client's queue pill, and the worker re-pushes the message without the flag
when it dequeues a queued item, turning the pill into a chat bubble;
injected messages are announced by the session itself at the moment they
enter history). After every turn the worker re-enqueues undelivered
injections. `stop`,
`new-chat`, and teardown release the pending question so
the blocked tool unwinds as a failed call. `GET /api/image/{client_id}`
serves pod images for inline rendering: only image files under `/workspace`
(png/jpg/jpeg/webp/gif) are eligible, sources over 50 MB are rejected, images
that fit the 1024x1024 box and stay under 5 MB are served untouched
(animations survive), everything else is downscaled into the box and
converted to PNG; each request gets a fresh copy (LRU-capped at 32) with
`Cache-Control: no-store`, so regenerated images are never served stale,
and a `download` query parameter serves the image as an attachment for the
UI's per-image download button.
A janitor coroutine reaps idle, disconnected sessions. `index()` serves
`chat.html` and injects `window.SYNA_CONFIG` (UI limits, voice availability,
the configured initial voice-mode state, and the per-process `auth_token`;
`</` is escaped so no value can close the script tag early). Speech
endpoints: `POST /api/speech/transcribe` (multipart clip to text) and
`POST /api/speech/synthesize` (JSON text to audio in the configured format;
'pcm' replies are wrapped in a WAV container server-side). `main()` runs
uvicorn.

Background work surfaces: after the pod starts, the session record
registers the registry's `event_sink` (subagent streaming as
non-persistent `subagent_message` events) and `completion_sink` (a
persistent `background_done` event plus the model-facing notice
enqueued into the inbox as a harness notice, waking the idle worker or
queueing behind an active turn — suppressed entirely when `wait_for`
already reported the outcome). `GET
/api/background/{client_id}/process/{id}/output?stdout_offset=&stderr_offset=`
returns incremental base64 chunks plus sizes, state, and `killed_by`
for the process viewer (tail runs before the size probe, so no chunk is
ever duplicated); `GET
/api/background/{client_id}/subagent/{id}/events` returns a subagent
viewer's history; `GET /api/background/{client_id}/list` returns every
registered process and subagent with its state and spawn time, oldest
first, for the top-bar indicator and its list modal. The WebSocket
accepts `process_kill` and
`subagent_kill` messages from the viewers' stop buttons, run off the
event loop. Teardown (`new-chat`, janitor, shutdown) stops running
subagents and drops the session's registry; the pod's processes die
with the pod itself.

Server auth (hardened boundaries): `main()` generates a per-process token
(`AUTH_TOKEN`, `secrets.token_urlsafe`); the HTTP middleware then requires
it on `/api/*` (header `X-Syna-Token` or `token` query; the query form
exists for `<img>` and download links, which cannot set headers) and
rejects requests whose `Host` header is not the configured loopback
binding (a DNS-rebound page's requests carry the attacker's domain). The
WebSocket endpoint rejects foreign `Origin` handshakes and requires the
token in the query (a missing Origin comes from the file:// fallback and
still needs the token; uvicorn's access log records the query string, so
the log file contains the token; local, process-private). CORS and the
WebSocket Origin check are pinned to the server's own UI origin
(`TRUSTED_ORIGINS`, the configured `server_port` on the loopback hosts):
another local port hosts some other application, whose pages must not
read the token-embedding HTML shell or open the WebSocket; the old
any-loopback-port regex allowed exactly that drive-by. `/` and
`/vendor/*` stay open so the page can bootstrap and learn the token. With
`AUTH_TOKEN` unset (embedded use, tests) every check is disabled; the
client sends `withToken()`/`authHeaders()` everywhere regardless. The
client id is sanitized in `get_or_create_session` (uploads included), not
just on the WebSocket path. Accepted residual risks (documented, not
fixed): the single shared browser means pages visited in one session
share profile/cookie state with others; from inside the pod, `curl` can
reach the podman bridge gateway interface of the host (the Syna server
binds loopback and is unreachable that way); pod egress policy is a
separate topic.

## 10. The web UI

The chat page and the setup wizard, built from `webui/` sources into
generated assets by `scripts/build.py`. Client narration lives in
[Voice and narration](#11-voice-and-narration).

### Sources and build

`src/syna/assets/chat.html` and `src/syna/assets/setup.html` are GENERATED by `scripts/build.py`: never edit them directly. The
sources live in `webui/`:

- `webui/index.html`: chat page skeleton with `{{CSS}}` and `{{JS}}` markers;
  `webui/index-setup.html` + `webui/css/setup.css` + `webui/js/setup.js` are
  the first-run setup wizard.
- `webui/css/`: style blocks (`base`, `ambient`, `chrome`, `chat`,
  `background`, `composer`, `voice`), inlined in that order. The UI is dark-only:
  `base.css` defines a single dark palette (`color-scheme: dark`), and
  no `prefers-color-scheme` branches remain anywhere in `webui/`.
- `webui/js/`: script blocks (`ambient`, `sfx`, `markdown`, `chat`,
  `speech_chunker`, `voice`), inlined in that order. The background light field (`ambient.js`)
  blends three states, `idle`, `thinking`, `tool`, through per-frame
  weight easing; while the model narrates aloud, `voice.js` pins the state
  to `tool` and releases it back to the computed state when the speech
  ends, so narration enters and exits with the same fluid crossfade. The
  activity AUDIO layer (`sfx.js` heartbeat/notes, driven by the same
  `data-ambient` attribute) stays silent while `Voice.speaking` is true:
  the voice is the only sound. Voice context pills ("Listening..." while
  recording, "Transcribing..." while the finished recording is being
  transcribed and sent: the pill the old flow dropped, leaving the user
  waiting in silence, and "Tuning up, please wait..." while a narrated
  reply's speech is being generated, before the reply's text is revealed)
  reuse the `.inline-status` styling with smooth grow-in/collapse
  transitions and are cleared on every fresh session.

`python scripts/build.py` replaces each marker with the corresponding source file
wrapped in a `<style>`/`<script>` tag. Two cascade/scoping gotchas:

- Cross-script communication must go through `window.*` properties: each JS
  source becomes its own `<script>` block, so a top-level `const` in one is a
  global lexical binding that other blocks can read by bare name but that
  does not exist as a `window` property (e.g. `voice.js` explicitly publishes
  `window.Voice`).
- Direct children of `#chat` are styled by `#chat > *` (`margin: 0 auto`,
  ID specificity). Any per-child rule that overrides those declarations must
  carry an ID too, or it silently loses the cascade; this bit the collapsed
  `.tool-overflow` wrapper's negative margin (see-more pill jump).
- Spacing constants must stay in sync by hand: `#chat { gap: 10px }`,
  `.tool-overflow { gap: 10px }`, its collapsed `margin-top: -10px`, and
  `CHAT_GAP = 10` in `js/chat.js` all encode the same row gap.
- `#chat > *` and `.tool-overflow > *` carry `flex: none`. Both containers
  are flex columns with a definite height, so any child that sets
  `overflow: hidden` (every height-animated element does, for clipping)
  would otherwise get an automatic minimum size of 0 and be flex-shrunk to
  the viewport instead of overflowing: height measurements taken mid-
  animation returned the shrunk size and the container "jumped" to its true
  height the moment the animation cleared `overflow` (the see-more expand
  and detail grow-in jumps). With `flex: none` the containers scroll, as
  their `overflow-y: auto` intends.

### Performance

- Rendering cost stays flat as conversations grow: chat children carry
  `content-visibility: auto` (offscreen messages skip layout, paint, and
  backdrop-filter), WebSocket history replay is batched into 8ms
  animation-frame slices with entrance animations suppressed until
  `history_done`, and per-event work is O(1) via tracked references
  (`runningToolRow`, `openFileGroup`, `thinkingRow`) instead of whole-chat
  DOM scans. The ambient canvas animation pauses on `visibilitychange`.
  Profiled before/after on a 120-turn synthetic history (Chromium trace):
  replay wall time 11.0s → 6.4s, JS time −73%, style recalc −81%, zero
  long tasks after the fix. Because a freshly inserted child measures at
  its intrinsic-size placeholder until the next rendering pass, `growIn`
  marks entering elements `.entering` (exempted from content-visibility)
  so entrance animations measure and reveal the real height. `.entering`
  also pins `transition-property` to `opacity, filter` while it is on:
  the `@starting-style` transform slide would otherwise run concurrently
  with `growIn`'s height/margin motion on a different curve and duration,
  kinking the entrance mid-flight. When motion is skipped (reduced
  motion, or nothing to reveal), `.entering` still covers one painted
  frame — dropping it immediately would let content-visibility show the
  next frame at the placeholder size, and the element would flash tall
  and snap to its real height. After `history_done` the WebSocket sends
  one live `busy`/`ready` event reflecting the worker's actual turn
  state: the persisted log ends on the last persistent event (often a
  "Thinking..." status notice, as busy/ready are non-persistent by
  design), which would otherwise leave the stale pill up forever after
  a reload.

### Client behavior

File uploads share one client path: the composer's attach button, a file
dropped anywhere on the window (a fixed `#drop-overlay` appears while a file
drag is over the page), and images pasted into the message box all feed the
same `uploadFiles()` helper, which posts each file to
`POST /api/upload/{client_id}`. Non-file drags (text, links) and directory
drops are ignored.

- Clicking a finished tool row (or a compaction row) toggles a
  `.tool-detail` container inserted after the row: one scrollable, capped
  section per data kind ("Call": the block's exact JSON, "Output": the
  tool result; "Summary" for compactions), each with its own copy button.
  Sections are created on click and destroyed on collapse (no persistent
  DOM, no fetches); the server caps each field at 6000 characters with an
  ellipsis, and the full text stays in the message dump and the log.
- Compaction summaries render as tool-call-style rows (`.compaction-row`,
  history icon) that join the surrounding tool run, so they hide under
  "See more" with 3+ consecutive rows and open the same detail view.
- "See more"/"See less" is a two-phase choreography with pre-measured
  heights: expanding animates only the wrapper height (the button glides,
  rows held invisible), then fades all rows in simultaneously; collapsing
  fades all rows out simultaneously (button in place), then glides the
  height closed. Interrupting a phase cancels it and reverses cleanly.
- All state-changing API calls carry the per-process server token
  (`withToken`/`authHeaders`): the WebSocket URL, fetches, inline pod
  images (`<img>` cannot set headers; query param), and file-download
  chips. The token arrives via `SYNA_CONFIG` from the served page.
- The composer carries an Allegro pill (`#allegro-btn`, `queue_music`
  icon + label) left of the send button: a speed-mode toggle remembered
  in localStorage (`syna-allegro`), collapsed to an icon-only circle
  below 640px. The flag rides every `message` event; the pill is
  disabled while busy. Toggling plays a dedicated ascending/descending
  Sfx pair (`allegroOn`/`allegroOff`, `ui` category) and re-triggers the
  shared `icon-pop` animation; both the sound and the animation respect
  the existing conventions (sound-button click exclusion, reduced-motion
  opt-out).
- The settings modal closes with a read-only collapsed "Allegro Mode"
  section (same `settings-details`/`settings-group` styling as Advanced
  Runtime Settings, one `.settings-note` paragraph): it states the
  Allegro runtime profile with live numbers computed from the loaded
  config. Nothing in it is editable; the mode is toggled from the
  composer.
- The Voice settings category carries a "Continuous conversation"
  dropdown (server config key `continuous_conversation`, default off,
  applied on Save and Restart like every other field): in voice mode,
  the microphone automatically reopens after each reply reaches the
  chat. A narrated reply arms the auto-listen when its text is revealed
  and opens the mic only when the narration drains; never while Syna
  is still speaking, which would record her own voice; an un-narrated
  reply opens it immediately. User-initiated stops (composer Stop,
  voice mode off, new chat) clear the armed auto-listen, and a reply
  that hears nothing does not loop: with no message sent there is no
  new reply to listen after.
- The composer area ends with a small centered cursive disclaimer:
  "Syna is an AI agent; it can make mistakes. Review its outputs and
  files before acting on them."
- Tool-row headers read the parsed `arguments` of the event (per-tool
  primary key) instead of regex-parsing the repr signature, which broke
  on commands containing single quotes (Python repr switches to double
  quotes and the first single-quoted span won, e.g. heredoc commands
  showing "EOF"). The signature parser remains only as a replay fallback.
  The dispatch tools (`process`, `subagent`) render their collapsed
  label from the call's `action` ("Spawned process", "Checked
  subagent", ...); for spawns without an explicit `name` the label
  starts bare and gains the auto-generated name from the spawn result
  (`structured_output`) when the row settles, matching the reported
  artifact paths.
- Images in Syna's replies (`![](/workspace/....png)`) render inline:
  `applyImagePolicy` rewrites pod-absolute sources to
  `GET /api/image/{client_id}?path=...` (lazy-loaded, with an
  "unavailable" fallback chip), while remote `http(s)` sources are
  converted to plain links and never fetched: a page could otherwise
  make Syna embed an image whose URL exfiltrates data. Pod images are
  also wrapped in an `.inline-image-frame` carrying a download button
  (revealed on frame hover, check-mark feedback like the copy buttons);
  clicking it navigates the same endpoint with `&download=1`, which the
  server answers with `Content-Disposition: attachment`. Message
  download/copy behavior is untouched.
- When Syna calls `ask_user`, the composer slides down and a question
  panel slides up in its place (choices capped in height and scrollable,
  an always-appended "Other: Type your own response" choice, and a stop
  button). Picking a choice answers the pending tool call; "Other"
  restores the composer in send mode so the next typed message resolves
  the question (the server intercepts it; no new turn starts). An empty
  choices array renders the question inline and keeps the composer in
  send mode. During history replay the question renders statically and
  is re-docked after `history_done` if still unanswered; stop, new chat,
  and disconnects all clear the panel.
- A message typed while the agent is busy never enters the chat. The
  server tags its `user_message` event with `queued: true`, and the
  client docks it as a pill right on top of the input field
  (`#queue-dock` in the footer, one `.queue-pill` per queued message:
  uppercase "Queued" label, truncated preview, a "Send now" button).
  Every message carries a client-generated id, echoed on every event
  about it, so the matching pill becomes a chat bubble the moment the
  message actually reaches history: either when the worker dequeues it
  for a new turn, or when a "Send now" injection lands mid-turn.
  "Send now" removes the pill's message from the queue and hands it to
  the session as a pending injection; the button disables until the
  delivery event arrives, and while Syna is waiting for an `ask_user`
  answer all Send now buttons disable with an explanatory tooltip, since
  the delivery can only happen after the question is resolved. Reconnects
  rebuild the dock from the event log
  (queued pill events first, their delivery event later); new chat and
  disconnects clear it. The old in-chat `.queued-tag` is gone.
- Background work has its own viewing layer (`background.css` plus a
  `chat.js` section). Clicking a finished `process`, `subagent`, or
  `wait_for` tool row opens a shared modal instead of the inline
  detail: the id comes from the spawn's structured output (`name` key)
  or the row's `name` argument (a `wait_for` row resolves its kind by
  asking the subagent endpoint first). The process view polls the output endpoint every
  second, appends base64-decoded chunks into two labeled panes
  (stdout/stderr) that auto-follow while pinned to the bottom and trim
  themselves past a cap; the subagent view fetches the recorded event
  history once, renders it as simplified message rectangles (goal,
  assistant text, compact tool rows), and appends live
  `subagent_message` events by sequence number. Both carry a
  full-width Stop button in a footer below the output, sending
  `process_kill`/`subagent_kill` over the WebSocket. The button doubles
  as the termination indicator: once the work ends it is disabled,
  restyled as a faded red dotted card, and shows the short outcome
  (`Stopped by user`, `Stopped by runtime cap`, `Exited 0`,
  `Ended with error`, `Finished`) — the header state chip hides then,
  so the outcome appears exactly once. Completion also arrives as a
  persistent `background_done` compact notice row (not a user bubble),
  clickable to reopen the viewer.
- A top-bar `list_alt` button left of the settings icon appears once
  the chat has spawned its first process or subagent and stays visible
  for the chat's lifetime (a new chat tears the registry down and
  clears it; so does closing the app). It is fed by the list endpoint
  (polled every 4s, refreshed instantly on spawn and completion
  events). It opens a two-tab "Background work" modal (Subagents /
  Processes) of rectangular cards sorted by spawn time — running items
  first, a separator, then finished items; each card shows the name, a
  command/goal snippet, and a state chip with the elapsed runtime, and
  clicking one opens the regular viewer modal for it.
- Tool-activity sound fades: a running tool row arms the wait fade
  (`Sfx.beginToolWait()` on the row, disarmed when it settles), and
  entering the tool ambient state with the fade armed schedules the
  tool-loop beat's fade to silence over 15 seconds; every tool-call
  event pokes `Sfx.poke()`, which ramps the gain back up and restarts
  the fade. A long `wait_for` goes quiet instead of looping; the next
  tool activity brings it back, and ambient state changes re-sync the
  gain. Narration reuses the tool ambience without arming the fade, so
  spoken replies keep the beat.

## 11. Voice and narration

Voice mode end to end. The server exposes transcription and synthesis
endpoints backed by OpenRouter's audio API (below); the browser records,
transcribes, and narrates replies aloud. The `voice.*` settings live in
the [Configuration](#3-configuration) tables, and the narration lab for
manual testing in [Testing](#12-testing).

### `src/syna/capabilities/speech.py`

Voice transcription and synthesis through OpenRouter's OpenAI-compatible
audio endpoints. `SpeechSettings` (frozen dataclass) carries the models,
voice, response format and locale from `.env`; `transcribe_audio` posts a
base64 clip to `/api/v1/audio/transcriptions` and returns text;
`synthesize_speech` posts text to `/api/v1/audio/speech` with the configured
`TTS_RESPONSE_FORMAT` and returns a `SynthesizedAudio` (bytes plus MIME
type); `wrap_pcm_in_wav` containers headerless 16-bit mono PCM samples
(24 kHz) into RIFF/WAVE so browsers can play them, passing through responses
that already carry a RIFF header; `audio_format_for` maps an upload MIME type
to an audio format and `audio_format_for_path` maps a file extension to the
same format for the `speech_to_text` tool; `transcribe_audio` takes an
explicit `language` hint (None omits the field so the model auto-detects);
`client_configuration` collects
the settings the web client needs for voice mode. `MAX_TTS_INPUT_CHARS`
(4000) bounds one synthesis request; measured against the endpoint:
~1000-character chunks work reliably, a ~19000-character request answered
with an opaque provider 502 ("empty audio stream"), and `text_to_speech`
rejects longer inputs up front with an error that says to split the file,
converting a mysterious provider failure into a one-step self-correction.
Failures raise
`SpeechError` with a user-safe message.

### Narration flow

Narrated replies no longer appear while their speech is being
generated. When a `model_message` will be narrated, the client calls
`Voice.narrate(content, reveal)`: the "Tuning up, please wait..."
pill appears immediately, _before_ the response exists in the chat,
and the bubble is rendered when the first chunk's audio arrives
(playback starts), the first chunk fails (error row), or the
configured `voice.narration_reveal_timeout_ms` (default 30 s)
elapses, whichever comes first; on timeout every in-flight synthesis
is aborted and its audio dropped. A newer narrated reply supersedes
a still-waiting one by revealing its text immediately. History
replay is unaffected: replayed messages render directly.

Long replies are synthesized as chunks, not as one request:
`speech_chunker.js` (`window.SpeechChunker`, DOM-free, also exported
for Node) reduces the speakable text to ordered chunks of at most
~1000 characters cut at sentence boundaries. Sentences come from the
ICU-backed `Intl.Segmenter`, which handles decimals, "a. m.",
ellipses, URLs, versions and inline enumerations natively; plus a
~28-entry merge list that re-glues ICU's single weakness, a title
abbreviation before a capitalized word ("El Dr. García"); engines
without `Intl.Segmenter` fall back to terminator scanning with the
same merge list. `speakableText` strips what must not be spoken:
code fences, images, raw URLs, LaTeX math (inline `$...$` only when
it carries LaTeX syntax, so "$3.50 - $5" survives), and emoji
including flags. `voice.js` dispatches chunks through a window of 2
in-flight synthesis requests and plays them strictly in order, the
first one the moment it is ready; synthesis runs several times
faster than playback (measured on Gemini TTS: ~12 s to synthesize a
~1000-char chunk against ~55 s of audio it produces, so the queue
cannot underrun). A chunk that errors, is
refused, or fails to decode is skipped with an event row while the
rest continues; the composer's Stop button, new chat, the ask-panel
stop, voice-mode off, and disconnects all call `Voice.stop()`, which
aborts in-flight synthesis, revokes the queued object URLs, and
silences playback.

## 12. Testing

All suites are plain scripts runnable with the project venv
(`.venv\Scripts\python.exe tests\<file>.py`) and require no Docker:

- `tests/test_json_protocol.py`: native protocol tests: tool schema generation
  from `tools.py` via `tool_schemas.py`, argument decoding and signature validation,
  batch stop-on-failure with skipped statuses, think tool results and signatures,
  structured output events, and channel sentinel/marker escaping. Also covers the
  `structured_output` event contract: dict-returning tools carry their raw return
  value on the finished event, string-returning tools do not, and
  `session.shared_file_metadata()` matches only well-formed finished `share_file` events.
  The double-escaped-quote repair pass is covered too: a GLM-shaped payload
  repairs and executes, strictly valid payloads are never rewritten, and
  unrepairable errors quote the raw arguments around the failure position.
- `tests/test_editor.py` - line-editor validation: valid and combined edits
  apply; null/non-object payloads, multi-key operations, missing required keys
  and unknown operation types raise clean `ValueError`s and leave the file
  untouched; CRLF style and a missing trailing newline are preserved;
  non-UTF-8 bytes survive unrelated edits byte-exactly; line numbering
  matches `grep -n` on form-feed content; and `replace_string` swaps the
  unique match, fails cleanly on zero or several matches without touching
  the file, and composes with line operations. Also covers the applied-diff
  return: a unified diff accompanies a successful edit, a no-change edit
  returns an empty diff, and a large diff is bounded to a head-and-tail
  view at `DIFF_MAX_CHARS`.
- `tests/test_tool_feedback.py`: session-level loop: history wiping after
  success, abort after 3 consecutive failures, `<tool_results>` marker
  format, parser warnings surfaced as harness notices, think+respond turns,
  step-budget warning (with the shared-files ledger, populated and empty),
  final status report delivered at exhaustion (and the
  error path when the final reply holds no valid `respond`), repeated-call
  nudges, compaction channel detection, and web event details.
- `tests/test_output_and_persistence.py`: `output.py` cleaning and
  head-and-tail rendering, plus `/tool_outputs/` persistence using a fake
  container (host tools persisted; pod-native `execute_command` not;
  failures degrade gracefully without `saved_to`).
- `tests/test_execute_command_scripts.py`: runs the exact pod-side
  runner/reader shell scripts generated by `execute_command` through a local
  real bash (Git Bash on Windows): exit-code capture, `(no output)`,
  head-and-tail marker, and timeout kill with partial output. Does not
  exercise bollard/Docker itself.
- `tests/test_command_timeout_notice.py`: the explicit timeout notice on
  `execute_command` results: a 124 kill appends the `TIMEOUT:` notice
  stating the exit code, the effective maximum runtime, and the
  background-job advice; 137 hedges between the timeout's final kill and a
  system kill; successful commands carry no notice; `command_runtime_limit_s`
  reports the deadline-bounded effective limit; and the hung-call notice
  names that limit for `execute_command` only.
- `tests/test_browser_error_text.py`: the browser's actionable Playwright
  error reporting: `describe_pw_error` keeps the call-log actionability
  verdict ("intercepts pointer events", "not visible") in the one-line
  `ERROR:` format, clips long DOM dump lines, caps the output, drops
  separator and "Call log:" header lines, and degrades to a placeholder
  on empty errors.
- `tests/test_browser_budgets.py`: the browser tool's own budgets and
  cancellation: a command within its budget returns its view, one slower
  than `browser_command_timeout_s` is cancelled mid-flight with a clear
  error, the harness's stop event cancels the in-flight command promptly,
  the screenshot capture runs under the same budget, both cancellation
  endings discard the shared session while a clean completion never does,
  the launch budget abandons the wait while the helper starts the browser,
  `invalidate` tears down off the callers' path and the next get starts
  clean, and the harness deadline composes launch + command + slack.
- `tests/test_request_payload.py`: verifies the request payload sent to
  OpenRouter carries the system prompt with real quote characters (no
  escaping-layer artifacts) and survives a JSON roundtrip unchanged.
- `tests/test_cache_routing.py`: endpoint-family cache routing:
  `sticky_routing_fields` per family (session_id / prompt_cache_key /
  nothing), `ask_question` applying the fields and reporting usage to
  the callback, and the session-end cache summary line (hit ratio
  math, silence when no requests were made).
- `tests/test_context_management.py`: token estimation (bounds, the
  script-aware CJK ratio, and hard-coded o200k tokenizer bounds),
  elision semantics
  (saved_to-only, recency window, idempotence), pod `HostConfig` assembly,
  the container self-heal (a stale-endpoint failure resets the client and
  retries once; a non-connection error propagates without a reset),
  a session-level elision-under-budget run, interval elision with per-request
  batching disabled, a session-level proof that outputs stay full-text
  between batched elision passes, and the append-only prefix-stability
  invariant across consecutive requests (frozen request snapshots).
- `tests/test_eval_scoring.py`: eval outcome scoring: honest failures,
  false successes, false failures, clarifying questions, no-report runs,
  and the headline rate computation.
- `tests/test_simulated_task.py`: an end-to-end scripted task: batched
  calls with a failure and a skip, think+retry, persistence, elision under
  the token budget, and a clean respond.
- `tests/test_ask_user.py`: the interaction bridge and tool: schema
  registration, responder round-trip and thread propagation into tool
  workers, no-responder and malformed-argument errors, cancellation
  (batch stops, remainder skipped), the `None`-timeout exemption (a slow
  question is not detached), `PendingQuestion` resolve/release semantics,
  and the terminal responder's number-or-text answer handling.
- `tests/test_queued_messages.py`: queued messages and "Send now":
  inbox items carry the client-generated id (and None on empty content),
  the deque/condition inbox keeps FIFO order while allowing per-id
  removal and wakes a waiting worker, the injection queue drains oldest
  first and survives concurrent writers, a turn where the running tool
  queues a "Send now" message lands it in history after the tool result
  and before the next model request (message order asserted, the second
  request payload carries it, and a `user_message` event with the id
  reaches the client), and undelivered injections re-enqueue after a
  turn ends.
- `tests/test_user_message_archives.py`: the archive tool and session hook: read-only pod write (exact bytes including LF newline
  preservation, mkdir + chmod 444 commands),
  failure returning None, large messages archived and annotated in
  `run_turn` (small and channel messages untouched, failed archives leave
  the turn unannotated but working, per-session archive ids differ), and
  the elision pass (head excerpt
  plus pointer, recency window, idempotence, plain/assistant messages
  ignored, markers with foreign archive ids rejected).
- `tests/test_review_work.py`: the verification tool: reviewer request
  shape (verifier prompt asset verbatim, labeled code-block log tail with
  truncation at `verify_log_tail_chars`, summary included, no tool
  schemas, no session id), model/effort selection with fallback and
  validation, no-provider/failed-reviewer/missing-model error dicts,
  schema registration, the `None`-timeout exemption in `parse.py`, the
  reviewer log filtering only the pending `review_work` call (siblings
  survive; a completed earlier review call stays), and a `run_turn`
  end-to-end run where history gains only the call, its result, and the
  final answer, with the provider popped afterward.
- `tests/test_paths_and_setup.py`: the filesystem layout and the setup
  wizard: bundled-asset resolution, `SYNA_HOME` override with first-run
  seeding, repo-root fallback, the endpoint-needs-key matrix, the
  `setup_incomplete` matrix (model/key/engine), `.env` merging with
  cache clearing, provider preset integrity, detection returning data
  instead of raising (including the registry PATH refresh on Windows),
  install failures returned as data, the sanity check requiring a
  reachable engine endpoint (not just a runnable binary), the wizard
  page served (and left)
  based on live setup state, and the setup API endpoints (token-gated
  detect, unknown dependency, key/validation errors on config).
- `tests/test_image_endpoint.py`: the inline image endpoint against a
  fake pod: path validation (outside `/workspace`, traversal, non-image
  extensions), pass-through for small in-box images (animated GIF bytes
  untouched), downscaling of oversized dimensions into the 1024x1024 PNG
  box, the 50 MB rejection, the recompression threshold, malformed-image
  rejection, the LRU cache eviction, and the `download=1` attachment
  header (default responses carry none).
- `tests/test_net_guard.py`: the browser's network policy: blocked-range
  matrix (loopback v4/v6/mapped, RFC1918, metadata, CGNAT, multicast),
  URL-spec obfuscations (decimal, hex, octal, mixed, trailing dot, and
  shortened dotted forms with large last labels, asserted against the
  correct ranges, not 0/8 luck), public short forms not blocked, hostname resolution blocked/allowed
  with stubbed `getaddrinfo`, resolution-failure pass-through, cache
  behavior, config overrides, scheme handling (ws/wss guarded, data:
  passed), `tool_visit` blocking before any page exists, and the route
  handler's neutralize/continue/abort outcomes plus the WebSocket
  observer.
- `tests/test_server_auth.py`: the local-origin and token enforcement:
  open API without a token, 401 without / 404 with the token (header and
  query), 403 on foreign Host headers, localhost accepted, shell and
  vendor staying open with the token injected, WebSocket handshake
  rejection for foreign Origins, other local ports (the pinned-origin
  rule), and missing tokens, acceptance of the server's own pinned
  origin, and CORS headers granted only to the UI origin.
- `tests/test_web_search_pacing.py`: search rate-limit pacing: concurrent
  searches never overlap, the gap survives separate non-overlapping calls,
  the pacing sleep is randomized within `[delay_s, 1.5*delay_s)` rather
  than a fixed value, the lock's wait cap raises a clear "appears stuck"
  error when the running search wedges past every budget, and the cap
  outlasts every legitimate hold (launch budget + search budget + grace).
- `tests/test_web_search_budgets.py`: the search's self-timed budgets: an
  engine chain slower than its budget stops with a clear timeout error, an
  expired launch budget abandons the wait while the constructor helper
  finishes and publishes the browser, a failed constructor propagates and
  the next call starts a fresh launch, concurrent first searches create
  exactly one browser, the pacing sleep ends early when the harness
  cancels the call, the goto timeout is exact against a frozen clock
  (ceiling kept, remaining-budget cap, expired-budget minimum), and the
  harness deadline outlasts lock queueing plus the searcher's own work.
- `tests/test_tool_cancellation.py`: the harness's terminal timeout
  semantics: a call finishing before its deadline returns normally, a
  tool's exception propagates, the cancel event reaches the worker, a
  `ToolCancelled` call renders as the "stopped, nothing is running"
  deadline notice, a call that finishes past its deadline within the stop
  grace delivers its real result, a worker ignoring the stop request is
  detached with the hung-call notice after the grace (still running at
  detach time), untimed calls wait for completion, the per-tool deadline
  dispatch matches the web-search composition, and the batch pipeline maps
  both timeout endings to failed `timeout` rows.
- `tests/test_web_search_parsing.py`: search result post-processing:
  DuckDuckGo `uddg=` and Bing base64 `/ck/a` redirect unwrapping (including
  plain links and malformed payloads), the title/URL/snippet block
  renderer, challenge-page detection (widget/marker probes), and the
  engine cooldown: challenged engines are paused and skipped, healthy
  engines behind a broken one keep serving.
- `tests/test_live_web_search.py`: live search chain (manual run): every
  extractor verified against markup fixtures in the real browser, live
  ddg-html/ddg-lite/bing required to return results (mojeek best-effort,
  it may serve an ALTCHA challenge, which the chain treats as no
  results), broken-first-engine fall-through, and graceful degradation
  when every engine is broken.
- `tests/js/test_speech_chunker.cjs`: zero-dependency Node suite for
  `webui/js/speech_chunker.js` (`node tests/js/test_speech_chunker.cjs`):
  the English and Spanish hard-case batteries (titles, decimals, "EE.
  UU.", enumerations, "p. ej.", "pág.", "etc."), the merge list and its
  chain behavior, the no-`Intl.Segmenter` fallback path (including its
  documented enumeration weakness), packing invariants (size caps,
  word-boundary hard splits, oversized single words, content and order
  preservation), degenerate inputs, content fidelity (emoji, HTML and
  sentinels pass through untouched), a ~6.4k-char news fixture with a
  100 ms performance bound, determinism, and the options/API contract.
- `tests/test_speech_chunker_js.py`: plain-script wrapper that runs the
  Node suite and mirrors its exit code; prints a skip when Node.js is
  not installed.
- `tests/test_tts_length_guard.py`: the `text_to_speech` input-length
  guard: text over `MAX_TTS_INPUT_CHARS` is rejected before synthesis
  with an error naming the cap and the split instruction, an under-limit
  text passes the guard and reaches synthesis, and the tool docstring
  states the same limit as the guard.
- `mock/`: the narration lab, a manual test rig: `uv run python
mock/mock_server.py` serves `mock/narration_lab.html` on
  http://127.0.0.1:8123/ together with the production
  `webui/js/speech_chunker.js`, loads three long fixtures (the real
  reply that failed to narrate, a markdown-heavy English report, a
  pathological mixed text) and reproduces the app pipeline:
  `speakableText`, chunking, the dispatch window, ordered playback,
  reveal timing, with per-chunk status rows and an event log. Synthesis
  runs either through a running Syna server (the per-process token is read automatically from the server's own page; a
  token pasted into the page overrides it) or as locally generated rising
  tones, so playback order is audible.
- `tests/_runner.py`: shared harness for the plain-script suites: a
  monkeypatch (attribute/mapping overrides with automatic undo) and
  `run_suite`, which executes every test, prints per-test results, and
  exits non-zero on failure.
- `evals/`: the live honesty eval suite (see `evals/README.md`): impossible
  and artifact tasks run against a real pod, scored mechanically, with the
  false-success rate tracked in `evals/results/history.jsonl`. Tasks include
  `impossible-visual-defect`, which plants a PDF whose table header row is
  shifted one column against its data, the defect observed in the first
  live Worlds run, and checks the agent renders the page (or admits it
  could not verify the layout) instead of calling the deliverable ready
  from a content-only check.
- `evals/run_worlds_benchmark.py`: THE LEAGUE OF LEGENDS WORLDS
  BENCHMARK. NOT a regular test and deliberately excluded from every
  run-all loop: it is not in `tests/` (no test runner discovers it) and
  not registered in `evals/tasks.py` (`run_evals.py` can never pick it
  up). Run it ONLY on significant project changes or on explicit user
  request: one live run takes 30-45 minutes, hundreds of model steps,
  and a fresh pod. It gives the agent one long multi-step task (a
  League of Legends Worlds PDF report) whose facts are scattered across
  many sites and API calls, saves the full transcript, the delivered
  artifacts (`shared/`), and this run's log slice to
  `evals/results/worlds_<timestamp>/`, and scores nothing; analysis is
  manual: tracing the final report back to the tool calls that produced
  each fact, watching for hallucination, loops, stalls, and dishonest
  success claims over very long histories. The wall-clock watchdog
  (default 40 min, `--timeout-s`) softly interrupts; runs also end
  early when the model responds, gives up, or errors. The step budget
  is raised (`--max-steps`, default 1000) so only the wall clock stops
  a run; compaction and elision stay at configured defaults, since
  exercising them over a huge history is the point. The runner refuses
  to start without `--i-understand-the-cost`: it prints a cost notice
  and exits with code 2 before touching Docker or the model, so an
  agent that runs every test after a change cannot start it blindly.
  An import-and-resolve guard (`tests/test_worlds_benchmark_runner.py`)
  imports the runner and resolves its configured log path, so refactors
  that break it surface in the regular suite instead of mid-launch.
- `tests/test_browser_health.py`: live browser health checks (run manually):
  Phase 1 reproduces the visit-read-screenshot-soak workflow against a heavy
  Fandom page and reports crashes, popup hijacks, navigations away, blank
  reads, or non-JPEG captures over 90 seconds; Phase 2 (`--stealth`) visits
  bot.sannysoft.com and CreepJS and dumps their verdicts for human review of
  the "functional and undetectable" standard.
- `tests/test_live_truthfulness.py`: live model evaluation scenarios
  (manual run, requires a container runtime and an API key).

## 13. Website

`website/` holds the static landing page deployed to
`julynx.github.io/syna` by `.github/workflows/deploy.yml` (GitHub Pages
from `./website` on every push to main). Plain HTML/CSS: navbar, hero
with the `uv tool install syna-agent` command (copy button with a
checkmark feedback animation and a legacy clipboard fallback), a feature
section rendering the README bullets verbatim as centered rows with
accent italic openers (justified body text on desktop, balanced centered
text on mobile), CTA, and footer; SEO metadata (OpenGraph, Twitter,
JSON-LD `SoftwareApplication` pointing at PyPI), `robots.txt` and
`sitemap.xml`. Brand assets: the transparent logo composited onto a dark
rounded square (`syna-logo-pill.png` for large sizes, `syna-mark-pill.png`
for small marks, `syna-favicon.png` as favicon). Screenshots are
placeholders until real ones are dropped into `website/assets/`.
