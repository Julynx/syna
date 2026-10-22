# Syna — Technical Description

## Overview

Syna is a terminal and web AI agent that answers user requests by driving an
LLM (through OpenRouter) in a native tool-calling loop. Tool schemas are
generated dynamically from `tools.py` function signatures and docstrings.
When the model invokes tools, the agent executes the requested calls
sequentially — the first failing call stops the batch and remaining calls are
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

## Running

- Web server & UI: `syna` (entry point `syna.server:main`, aliases: `syna-agent`,
  `syna-server`) — FastAPI + WebSocket chat UI served from the bundled `chat.html`.
  Automatically opens the first-run setup wizard if setup is incomplete.
- CLI: `syna-cli` (entry point `syna:main`) — interactive prompt `(Ask Syna):`,
  supports slash commands. Exits cleanly with guidance if configuration is missing.
- Setup wizard: `syna-setup` (entry point `syna.setup:main`) — starts the
  web server and opens the first-run wizard at `/setup`.
- Web UI sources live in `build/` (see below); after editing them, regenerate
  the UIs with `python scripts/build.py` — `src/syna/assets/chat.html` and
  `src/syna/assets/setup.html` are generated files and
  must not be edited directly.
- Install from PyPI with `uv tool install syna-agent` (or
  `pip install syna-agent`); for development, `uv sync`; Python >= 3.12
  required.

## Filesystem layout

Read-only assets (prompts, config defaults, web UIs, vendor files) are
bundled inside the package at `src/syna/assets/` and loaded through
`paths.py` (`importlib.resources`; `asset_path` / `read_asset_text`).
Everything the user writes lives in the Syna data home (`~/.syna`,
overridable with `SYNA_HOME`): `.env`, `config.json` (seeded from the
bundled defaults on first run), `syna.log`, and session dumps. In a
repository checkout the repo root acts as the data home so development
workflows keep working unchanged.

## Dependencies

- `openrouter` — model client with configurable timeout/retry.
- `python-dotenv` — loads `OPENROUTER_API_KEY` and model settings from `.env`.
- `httpx` — OpenRouter audio endpoints (speech transcription and synthesis).
- `bollard` — Docker/Podman container client used for the sandbox.
- `playwright`, `drissionpage`, `camoufox`, `markdownify` — browser-based web
  search and page-to-markdown conversion, plus the persistent stealth browser
  session.
- `pillow` — image downscaling for the vision pipeline.
- `fastapi`, `uvicorn`, `python-multipart` — web server and uploads.

## Configuration

`.env` holds the secrets and model selection:

| Key | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | API key for the configured endpoint (any OpenAI-compatible provider's key goes here; the variable name predates multi-endpoint support). |
| `MODEL` | Main agent model (default `~z-ai/glm-flash-latest` when unset). |
| `BASE_URL` | OpenAI-like endpoint URL (default `https://openrouter.ai/api/v1`; also OpenAI, Gemini's compatibility layer, a local Ollama, or any compatible endpoint). Image generation and the speech tools are OpenRouter-only and report themselves as not configured elsewhere. |
| `STT_MODEL` | Voice transcription model (default `google/chirp-3` when unset; gated by the `voice_enabled` toggle). |
| `TTS_MODEL` | Speech synthesis model (default `google/gemini-3.8-flash-lite-tts` when unset; gated by the `voice_enabled` toggle). |
| `TTS_VOICE` | Synthesis voice (default `Kore`). |
| `TTS_RESPONSE_FORMAT` | Speech endpoint response format (default `pcm`; raw samples are wrapped in a WAV container server-side). Some models, e.g. OpenAI TTS, also accept `mp3`. |
| `IMAGE_MODEL` | Image generation model for the `text_to_image` tool (default `google/gemini-3.1-flash-image` when unset; the tool only runs when the `image_generation_enabled` toggle is on). |
| `VERIFIER_MODEL` | Reviewer model for the `review_work` tool (defaults to `MODEL`, then the default model, when unset). |
| `VERIFIER_MODEL_EFFORT` | Reasoning effort for the reviewer request (default `high`). |
| `SPEECH_LANGUAGE` | Spoken language hint for STT (default `en`). |

`assets/config.json` holds all runtime settings:

| Key | Purpose |
| --- | --- |
| `timeout_ms` | Model request timeout. |
| `retry` | Backoff strategy for model requests. |
| `max_command_output_chars` | Character limit at which tool output is cut to its beginning and end (the system prompt's `{{OUTPUT_LIMIT}}`). The full output always remains available at the `saved_to` path. |
| `tool_call_timeout_s` | Per-tool-call timeout (default 60). A call that exceeds it is detached, not killed; see `parse.py`. |
| `command_timeout_s` | Per-`execute_command` timeout (default 50, kept below `tool_call_timeout_s`). The command is wrapped in `timeout -k 5` inside the pod so hangs are killed instead of detaching; long-running work belongs in background jobs (`nohup ... &`), as the system prompt documents. |
| `pod_memory_mb` | Pod memory ceiling in MB (default 8192). Protects the host from runaway processes inside the pod; does not restrict what the model may do inside it. |
| `pod_pids_limit` | Pod process-count ceiling (default 512), guarding against fork bombs. |
| `max_steps_per_turn` | Model requests allowed per turn (default 100). Before the cap, the model receives a wrap-up warning; past it, the turn ends with one final request reserved for a `respond` status report. |
| `step_warning_remaining` | Remaining-steps count that triggers the wrap-up warning (default 20). |
| `context_budget_tokens` | Estimated-token budget for the whole conversation (default 100000). When exceeded, old tool outputs are elided first and summarization runs only if the budget is still exceeded. |
| `elide_keep_recent_messages` | History messages kept untouched by output elision (default 12). |
| `elide_interval_messages` | History size at which lossless output elision runs on its own, instead of waiting for the token budget (default 30). Keeps prompts small through long runs; elided outputs remain readable in the pod at their `saved_to` paths. |
| `elide_every_requests` | Requests between batched elision passes once the message interval is reached (default 10). Batching keeps the conversation prefix stable between passes so the provider prompt cache stays warm; the token-budget path always elides immediately. |
| `elide_user_message_head_chars` | How much of an archived large user message stays in history after elision (default 300); the rest is replaced by a pointer to the verbatim archive file. |
| `user_message_archive_min_chars` | User message length at which the message is archived verbatim into the pod and annotated with its path (default 2000). |
| `user_messages_dest` | Pod directory holding the archived user messages (default `/user_messages`). |
| `verify_log_tail_chars` | How many characters of the serialized conversation log the `review_work` reviewer receives (default 128000). |
| `max_history_messages` | History length that triggers compaction. |
| `enable_compaction` | Master switch for history compaction. |
| `max_visible_tool_calls` | UI limit on rendered tool rows. |
| `voice_enabled` | Voice category toggle (default `true`): off hides the mic, disables the top-bar voice icon, and makes the speech tools report themselves as unavailable. |
| `image_generation_enabled` | Image generation toggle (default `false`): off makes `text_to_image` report itself as unavailable; on, the model may generate images. |
| `log_file` | Log file name (written to the project root). |
| `session_dump_file` | Per-session message dump file name template. |
| `server_host`, `server_port` | Web server binding. |
| `session_idle_timeout_min` | Idle web sessions are reaped after this. |
| `upload_dest` | Default upload destination inside the pod. |
| `voice.silence_timeout_ms` | Mic silence that ends a voice recording. |
| `voice.silence_threshold_dbfs` | Loudness level treated as silence. |
| `voice.max_recording_ms` | Hard cap on one voice recording. |
| `voice.voice_mode_on` | Initial voice-mode state pushed to the client on every fresh session (page load, new chat, server restart). Accepts booleans and "true"/"false" strings; voice never persists across sessions. |

Static assets are bundled in `src/syna/assets/`: `prompt.md` (system prompt template with a
`{tools}` placeholder where live tool documentation is injected, plus the
`{{CURRENT_DATE}}`, `{{COMMAND_TIMEOUT}}` and `{{OUTPUT_LIMIT}}` placeholders
filled at build time), `verifier_prompt.md` (system prompt for the isolated
`review_work` reviewer), `compaction_prompt.md`, `chat.html` and `setup.html`
(web UIs), and `vendor/` (markdown-it, KaTeX, fonts, icons, logo).

## Web UI build system

`src/syna/assets/chat.html` and `src/syna/assets/setup.html` are GENERATED by `scripts/build.py` — never edit them directly. The
sources live in `build/`:

- `build/index.html` — chat page skeleton with `{{CSS}}` and `{{JS}}` markers;
  `build/index-setup.html` + `build/css/setup.css` + `build/js/setup.js` are
  the first-run setup wizard.
- `build/css/` — style blocks (`base`, `ambient`, `chrome`, `chat`,
  `composer`, `voice`), inlined in that order.
- `build/js/` — script blocks (`ambient`, `sfx`, `markdown`, `chat`,
  `voice`), inlined in that order. The background light field (`ambient.js`)
  blends three states — `idle`, `thinking`, `tool` — through per-frame
  weight easing; while the model narrates aloud, `voice.js` pins the state
  to `tool` and releases it back to the computed state when the speech
  ends, so narration enters and exits with the same fluid crossfade. The
  activity AUDIO layer (`sfx.js` heartbeat/notes, driven by the same
  `data-ambient` attribute) stays silent while `Voice.speaking` is true —
  the voice is the only sound. Voice context pills ("Listening..." while
  recording, "Tuning up, please wait..." between a narrated reply and its
  playback) reuse the `.inline-status` styling with smooth grow-in/collapse
  transitions and are cleared on every fresh session.

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
  so entrance animations measure and reveal the real height.

File uploads share one client path: the composer's attach button, a file
dropped anywhere on the window (a fixed `#drop-overlay` appears while a file
drag is over the page), and images pasted into the message box all feed the
same `uploadFiles()` helper, which posts each file to
`POST /api/upload/{client_id}`. Non-file drags (text, links) and directory
drops are ignored.

`python scripts/build.py` replaces each marker with the corresponding source file
wrapped in a `<style>`/`<script>` tag. Two cascade/scoping gotchas:

- Cross-script communication must go through `window.*` properties: each JS
  source becomes its own `<script>` block, so a top-level `const` in one is a
  global lexical binding that other blocks can read by bare name but that
  does not exist as a `window` property (e.g. `voice.js` explicitly publishes
  `window.Voice`).
- Direct children of `#chat` are styled by `#chat > *` (`margin: 0 auto`,
  ID specificity). Any per-child rule that overrides those declarations must
  carry an ID too, or it silently loses the cascade — this bit the collapsed
  `.tool-overflow` wrapper's negative margin (see-more pill jump).
- Spacing constants must stay in sync by hand: `#chat { gap: 10px }`,
  `.tool-overflow { gap: 10px }`, its collapsed `margin-top: -10px`, and
  `CHAT_GAP = 10` in `js/chat.js` all encode the same row gap.
- `#chat > *` and `.tool-overflow > *` carry `flex: none`. Both containers
  are flex columns with a definite height, so any child that sets
  `overflow: hidden` (every height-animated element does, for clipping)
  would otherwise get an automatic minimum size of 0 and be flex-shrunk to
  the viewport instead of overflowing — height measurements taken mid-
  animation returned the shrunk size and the container "jumped" to its true
  height the moment the animation cleared `overflow` (the see-more expand
  and detail grow-in jumps). With `flex: none` the containers scroll, as
  their `overflow-y: auto` intends.

Client behavior notes:

- Clicking a finished tool row (or a compaction row) toggles a
  `.tool-detail` container inserted after the row: one scrollable, capped
  section per data kind ("Call" — the block's exact JSON, "Output" — the
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
  images (`<img>` cannot set headers — query param), and file-download
  chips. The token arrives via `SYNA_CONFIG` from the served page.
- The composer area ends with a small centered cursive disclaimer:
  "Syna is an AI agent; it can make mistakes. Review its outputs and
  files before acting on them."
- Tool-row headers read the parsed `arguments` of the event (per-tool
  primary key) instead of regex-parsing the repr signature, which broke
  on commands containing single quotes (Python repr switches to double
  quotes and the first single-quoted span won — e.g. heredoc commands
  showing "EOF"). The signature parser remains only as a replay fallback.
- Images in Syna's replies (`![](/workspace/....png)`) render inline:
  `applyImagePolicy` rewrites pod-absolute sources to
  `GET /api/image/{client_id}?path=...` (lazy-loaded, with an
  "unavailable" fallback chip), while remote `http(s)` sources are
  converted to plain links and never fetched — a page could otherwise
  make Syna embed an image whose URL exfiltrates data.
- When Syna calls `ask_user`, the composer slides down and a question
  panel slides up in its place (choices capped in height and scrollable,
  an always-appended "Other — type your own response" choice, and a stop
  button). Picking a choice answers the pending tool call; "Other"
  restores the composer in send mode so the next typed message resolves
  the question (the server intercepts it — no new turn starts). An empty
  choices array renders the question inline and keeps the composer in
  send mode. During history replay the question renders statically and
  is re-docked after `history_done` if still unanswered; stop, new chat,
  and disconnects all clear the panel.

## Landing page

`website/` holds the static landing page deployed to
`julynx.github.io/syna` by `.github/workflows/deploy.yml` (GitHub Pages
from `./website` on every push to main). Plain HTML/CSS: navbar, hero
with the `uv tool install syna-agent` command, a six-card feature grid,
CTA, and footer; SEO metadata (OpenGraph, Twitter, JSON-LD
`SoftwareApplication` pointing at PyPI), `robots.txt` and
`sitemap.xml`. Brand assets: the transparent logo composited onto a dark
rounded square (`syna-logo-pill.png` for large sizes, `syna-mark-pill.png`
for small marks, `syna-favicon.png` as favicon). Screenshots are
placeholders until real ones are dropped into `website/assets/`.

## File structure and module responsibilities

### `src/syna/__init__.py`
CLI entry point. `main()` starts the ask loop, handles Ctrl+C, and closes the
global Docker client on exit.

### `src/syna/ask.py`
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

### `src/syna/browser.py`
Persistent stealth browser session (Camoufox) exposed as LLM-callable actions.
Renders pages through an accessibility-style walker that emits a compact
line-based view (URL, title, scroll position and numbered interactables), and
dispatches commands (`goto`, `click`, `type`, ...) by numeric element ref.
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
failed requests are logged to the dev log.

Network guard: the browser runs on the host, so all its traffic is
host-originated. A single `context.route("**/*")` handler (`_guard_route`)
is the choke point for every request — ad/identity neutralization and the
`net_guard` policy (public internet only) in one place, independent of
Playwright's route-matching order. WebSocket handshakes cannot be blocked
by routing, so `_on_websocket` kills sockets to non-public destinations the
moment they appear (the Syna server's Origin check is the second layer).
`tool_goto` additionally validates the destination before navigating with a
model-readable error. WebRTC is disabled via `firefox_user_prefs` (it
bypasses HTTP-layer interception and is a standard IP-leak/fingerprint
surface). Note: interception in this Camoufox build pins the browser to
HTTP/2 — HTTP/3 is unavailable in both route and proxy modes (verified
against cloudflare-quic.com); absence of H3 is a common real-world pattern.

### `src/syna/net_guard.py`
The browser's network policy: public internet targets only. Blocked ranges
(loopback, RFC1918, link-local incl. the cloud metadata service, CGNAT,
ULA, multicast, reserved, documentation) are an explicit list, independent
of `ipaddress.is_global` version drift. `check_destination` parses IP
literals with the WHATWG URL IPv4 semantics browsers actually apply
(decimal, hex, octal, mixed, trailing dot, shortened dotted forms where
the last label carries the remaining bits — e.g. "127.65535" is
127.0.255.255), and resolves hostnames at decision time
with a short-lived cache, blocking names that resolve to any private
address — which closes casual DNS rebinding; a fast-flip rebinding
adversary can still race the browser's own second resolution, which is
why the Syna server independently validates the Host header, Origin, and
token. Resolution failures pass (the
connection fails anyway). `check_url` guards http/https/ws/wss and lets
scheme-less payloads (data:, about:) through. Config:
`browser_allow_private_networks` (default false) and
`browser_allowed_hosts` (exact hostnames, e.g. a local dev server).

### `tests/test_browser_health.py`
Live browser health checks (run manually): Phase 1 reproduces the
goto-read-screenshot-soak workflow against a heavy Fandom page and reports
crashes, popup hijacks, navigations away, blank reads, or non-JPEG captures
over 90 seconds; Phase 2
(`--stealth`) visits bot.sannysoft.com and CreepJS and dumps their verdicts
for human review of the "functional and undetectable" standard.

### `src/syna/channel.py`
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

### `src/syna/ask_utils.py`
Shared helpers: `show_welcome()` banner and `ask_question(client, model,
messages, delay, effort, dump_path, response_format, accept_reply,
session_id, tools, tool_choice)` — sends model requests with configurable delay,
request dumps, and native `tools` and `tool_choice` parameters. Every request
carries `max_tokens` from the `max_output_tokens` config value (default
16384), so reasoning models cannot starve the visible reply of the
provider's default completion budget; `finish_reason` and the
prompt/completion/reasoning/cached token split are logged for every
response. When a `session_id` is given it is sent with every request so
OpenRouter pins provider sticky routing and the provider-side prompt cache
stays warm for the whole conversation. Accepts an optional `accept_reply`
validator that raises `ReplyRejected` on channel impersonation in content or
tool call arguments. Transient empty responses are retried up to
`EMPTY_RESPONSE_MAX_ATTEMPTS` times.

### `src/syna/commands.py`
User slash commands (`/send`, `/receive`, `/help`, `/bye`) executed locally on
the host. `send`/`receive` copy files between host and pod through the
thread's container; they open native file dialogs via tkinter, so the CLI's
file-transfer commands require a desktop session. `parse_and_execute_command`
validates and dispatches.

### `src/syna/compaction.py`
History-compaction support: loads the compaction prompt, renders history as a
labeled transcript (`[user]`, `[tool_results]`, `[harness_notice]`,
`[assistant]`), and builds the summarization request. Compaction summaries are
wrapped with the `COMPACTION` sentinel channel and `<harness_notice>` tags
(`format_compaction_message`) and detected via `is_summary_message` so repeated
compactions merge rather than duplicate.

### `src/syna/context.py`
Token estimation and elision for context management:
`estimate_tokens` / `estimate_history_tokens` (characters/4 heuristic, no
tokenizer dependency), `elide_tool_outputs` — replaces old result
outputs that carry a `saved_to` path with a one-line pointer to the pod
file, leaving the system message, the most recent messages, and anything
unrecoverable untouched — and `elide_archived_user_messages`, which
replaces old archived user messages (marked by `user_messages.py`, and
only when the marker carries the session's archive id) with
a head excerpt of `elide_user_message_head_chars` characters plus a
pointer to the verbatim archive file. Both passes are idempotent.

### `src/syna/config.py`
Configuration and logging: `load_config()` (reads `assets/config.json`),
`get_logger()` (file logger), `get_project_root()`, and loaders for the
protocol limits the prompt and tools consume: `load_output_limit_chars()`
(head-and-tail cut), `load_command_timeout_s()` and
`load_tool_call_timeout_s()`.

### `src/syna/editor.py`
Read/edit primitives used by the `read_file` and `edit_file` tools. Files are
read and written as UTF-8 (undecodable bytes replaced), so pod files with
non-ASCII content survive host-side processing. Edit operations are validated
before the file is touched: non-dict or multi-key
operations, non-object payloads, missing required keys, unknown operation
types, out-of-bounds line numbers, invalid ranges and overlapping ranges all
raise clean `ValueError`s instead of leaking internal TypeErrors.

### `src/syna/image_generation.py`
Text-to-image generation through OpenRouter's dedicated Image API
(`POST /api/v1/images`). `ImageSettings` (frozen dataclass) carries the
model from `.env` (`IMAGE_MODEL`); when unset the caller reports the feature
as not configured by the user. `generate_image(prompt, settings)` posts the
prompt, decodes `data[0].b64_json`, and returns a `GeneratedImage` (bytes
plus media type; `image/png` when the provider omits it, `extension` maps
the media type to a file extension). The API bills all-or-nothing, so
failures (non-200, malformed response) raise `ImageGenerationError` with a
user-safe message carrying the provider detail.

### `src/syna/output.py`
Cleaning and bounded rendering of tool output shown to the model:
`strip_ansi_escapes`, `collapse_progress_lines` (carriage-return progress
updates reduce to their last segment), `head_and_tail` (keeps the beginning
and end of long output with an explicit omission marker pointing at the
`saved_to` file), and `bounded_output` (cleaning plus cut plus an explicit
`(no output)` placeholder for empty results).

### `src/syna/imaging.py`
Image processing shared by the vision pipeline and the web server's inline
image endpoint. `fit_image_to_box` writes a PNG copy of a source image
downscaled into a 1024x1024 box (CMYK converted to RGB first);
`image_exceeds_box` tests an open Pillow image against the box;
`MAX_SERVE_BYTES` (50 MB) and `RECOMPRESS_BYTES` (5 MB) bound what the
image endpoint will serve.

### `src/syna/inspect_module.py`
Extracts function names, docstrings and argument metadata via `ast`; used to
inject live tool documentation into the system prompt and to validate tool
call arguments in `parse.py`. Each argument carries its name, type
annotation, whether it has a default, and the default's source text.

### `src/syna/tool_schemas.py`
Generates OpenRouter/OpenAI-compatible tool definitions from `tools.py` via
`ast` function metadata. Converts Python type annotations to JSON Schema
properties (`str` -> `string`, `int` -> `integer`, `list` -> `array`, `dict` -> `object`),
excludes defaults from `required`, and parses `- `arg` (type, required): ...`
docstring lines into property descriptions while stripping them from the tool
description. `get_tool_definitions()` returns the schemas for all registered
tools. Note: any `- `-prefixed docstring line is treated as an argument
description, so non-argument bullet lists in tool docstrings must use `* `
bullets (see `web_interact`) or they are pulled out of the tool description.

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
`detect_docker` / `detect_engine` (PATH probes plus `--version`), and
`detect_all()` for the wizard's dependency step. Installation is
best-effort per platform (`winget` on Windows, package managers
elsewhere) with the log tail returned for the wizard's error box; WSL
reports `reboot_required`. `write_env_file` merges keys into the data
home `.env` and clears the env cache; `endpoint_needs_key` exempts
local endpoints; `setup_incomplete()` (checked live on every request,
so finishing the wizard flips the server in-process) requires a key
unless local, an engine, and treats a missing MODEL as satisfied since
the runtime falls back to `config.DEFAULT_MODEL`; `sanity_check()` runs
the engine probe and a one-token chat completion against the configured
endpoint. `PROVIDERS` holds the preset base URLs, key-page links, and
default models (OpenRouter, OpenAI, Google Gemini, Ollama), sourced
from the canonical constants in `config.py`.

### `src/syna/parse.py`
Tool-call parsing and execution under native OpenRouter tool calling.

- `build_signature(tool_name, tool_arguments)` — one-line python-like
  rendering of a call.
- `load_tool_call_timeout()` — configured `tool_call_timeout_s` (default 60).
- `_unpack_tool_call(raw_call)` — extracts call ID, tool name, decodes JSON
  arguments string or dict, and reports argument decoding errors.
- `ParseOutcome(structural_error, outputs, warnings, think_signatures)` — returns
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
- Browser command failures (`web_interact` views starting with "ERROR:")
  record exit code 1, so they surface as `error` rows and stop the batch;
  `read_file`/`edit_file` translate Docker 404s into clean "No such file or
  directory" errors with exit code 1, while other Docker errors propagate
  unchanged.
- `execute_tool_with_timeout(tool_name, tool_arguments, timeout_s,
  output_path)` — runs the tool in a daemon worker thread, propagating the
  calling thread's registered container, question responder, conversation-log
  provider (`review_work`'s reviewer snapshot), and the call's
  output path into the worker (all thread-locals), and reports back the
  tool-recorded exit code. On timeout the worker is detached and a notice is
  returned as the tool output with status `timeout`. `ask_user` and
  `review_work` are dispatched
  with a `None` timeout so a pending question is never detached, and the
  reviewer request is bounded by the model client's own timeout/retry
  policy instead.
- `_persist_tool_output(output_text, saved_to, persist_outputs)` — saves
  host-side tool output (web tools, file tools, ...) into the pod at
  `/tool_outputs/` via `copy_to` and chmods the file read-only;
  `execute_command` writes its own file
  pod-side instead. Web sessions persist through the thread's bound
  container; CLI sessions opt in via `persist_outputs` and use the shared
  pod. When no container can be used (unit tests, persistence errors) the
  output is still delivered — only the `saved_to` attribute is dropped.
- Output shown to the model is cleaned (ANSI stripped, progress lines
  collapsed, empty -> `(no output)`) and cut to head-and-tail at
  `max_command_output_chars`.
- `parse_and_execute_tools(tool_calls, on_tool_event, echo_errors,
  tool_timeout_s)` — validates arguments against declared signatures; emits
  `started` / `finished` / `error` / `skipped` events with `tool_call_id`,
  `raw` JSON, and parsed `arguments` for front-end call details; `failed=True` is
  passed on finished events for failed runs so the UI can style them.

### `src/syna/server.py`
FastAPI app. One `SessionRecord` per client id owns an `AgentSession`, a
dedicated container (registered thread-locally in the record's worker thread),
event queues, an event log for WebSocket replay, an `ask_user` pending
question, and shared-file downloads (when a finished `share_file` event
carries `structured_output` metadata — matched via
`session.shared_file_metadata()` — the record copies the pod path to the host
and pushes a `file` event the client renders as a download; worker teardown
releases the container and model client; `share_file` recreates the session
temp dir if it was removed externally). During a turn the record registers an
`ask_user` responder that pushes nothing extra (the client renders the
question from the tool call's `started` arguments) and blocks on the pending
answer queue; the WebSocket handler resolves the question on an `answer`
message or treats a plain `message` as the typed answer instead of queueing a
turn, and `stop`, `new-chat`, and teardown release the pending question so
the blocked tool unwinds as a failed call. `GET /api/image/{client_id}`
serves pod images for inline rendering: only image files under `/workspace`
(png/jpg/jpeg/webp/gif) are eligible, sources over 50 MB are rejected, images
that fit the 1024x1024 box and stay under 5 MB are served untouched
(animations survive), everything else is downscaled into the box and
converted to PNG; each request gets a fresh copy (LRU-capped at 32) with
`Cache-Control: no-store`, so regenerated images are never served stale.
A janitor coroutine reaps idle, disconnected sessions. `index()` serves
`chat.html` and injects `window.SYNA_CONFIG` (UI limits, voice availability,
the configured initial voice-mode state, and the per-process `auth_token`;
`</` is escaped so no value can close the script tag early). Speech
endpoints: `POST /api/speech/transcribe` (multipart clip to text) and
`POST /api/speech/synthesize` (JSON text to audio in the configured format;
'pcm' replies are wrapped in a WAV container server-side). `main()` runs
uvicorn.

Server auth (hardened boundaries): `main()` generates a per-process token
(`AUTH_TOKEN`, `secrets.token_urlsafe`); the HTTP middleware then requires
it on `/api/*` (header `X-Syna-Token` or `token` query — the query form
exists for `<img>` and download links, which cannot set headers) and
rejects requests whose `Host` header is not the configured loopback
binding (a DNS-rebound page's requests carry the attacker's domain). The
WebSocket endpoint rejects foreign `Origin` handshakes and requires the
token in the query (a missing Origin comes from the file:// fallback and
still needs the token; uvicorn's access log records the query string, so
the log file contains the token — local, process-private). `/` and
`/vendor/*` stay open so the page can bootstrap and learn the token. With
`AUTH_TOKEN` unset (embedded use, tests) every check is disabled; the
client sends `withToken()`/`authHeaders()` everywhere regardless. The
client id is sanitized in `get_or_create_session` (uploads included), not
just on the WebSocket path. Accepted residual risks (documented, not
fixed): the single shared browser means pages visited in one session
share profile/cookie state with others; from inside the pod, `curl` can
reach the podman bridge gateway interface of the host (the Syna server
binds loopback and is unreachable that way) — pod egress policy is a
separate topic.

### `src/syna/speech.py`
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
the settings the web client needs for voice mode. Failures raise
`SpeechError` with a user-safe message.

### `src/syna/session.py`
`AgentSession` — the shared conversation loop for CLI and web.

- Builds the system prompt from `assets/prompt.md`: injects the live tool
  documentation (docstrings from `tools.py`) at `{tools}` and fills
  `{{CURRENT_DATE}}`, `{{COMMAND_TIMEOUT}}` and `{{OUTPUT_LIMIT}}`.
- Loads dynamic tool schemas via `get_tool_definitions()` from `tool_schemas.py`.
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
  any call left hanging by an abnormal exit — user interruption mid-batch,
  an internal batch error, or a stray call beside the final status report —
  with synthetic `skipped` results, and fills in missing call ids in place.
  Tool-call ids absent from a provider reply are synthesized at the source
  (`ask_utils.py`) and in the parser backstop, keeping every pair consistent.
- **Sentinel channel separation**: tool results are appended as individual
  `role: "tool"` messages wrapped with `format_tool_result_content` on the
  `TOOL_RESULTS` channel — containing a `<result>` element with status, exit code,
  and pod path — corrective warnings, file uploads, step-budget warnings and
  interruptions as `HARNESS_NOTICE`, and compaction summaries as `COMPACTION`.
- **Two-stage context management** (`context.py`): once the history reaches
  `elide_interval_messages`, and additionally whenever estimated tokens
  exceed `context_budget_tokens`, old tool outputs (those with `saved_to`)
  outside the recent window are elided to pointers — lossless, since the
  model can re-read them in the pod. Archived large user messages are
  elided the same way (head excerpt plus pointer to the verbatim file).
  To keep the provider prompt cache warm,
  the rewrite is batched: outside the budget-exceeded path it only runs every
  `elide_every_requests` requests, so history stays append-only (and the
  cache prefix intact) between passes. Every output still becomes a pointer
  eventually. Summarization runs only if the message count reaches
  `max_history_messages` or the budget is still exceeded after elision,
  keeping the system prompt, first real user message, the compaction
  summary, and the tail atomic message slice.
- **Step budget**: each turn allows `max_steps_per_turn` model requests; when
  `step_warning_remaining` steps are left, the model receives a wrap-up
  notice. Past the cap the turn ends with one final model request with
  `tool_choice="none"`, delivering the status report as natural language; an
  empty report ends the turn with the plain exhaustion error.
- **Loop detection**: identical non-`think` calls repeated
  `REPEATED_CALL_LIMIT` (3) times within one turn trigger a one-time
  "change approach" harness notice; the same guard covers identical think
  notes (lone or batched).
- **Lone-think acceptance**: a reply containing only `think` calls is
  accepted rather than bounced — the reply persists in history, pending
  corrective notices from earlier malformed attempts are purged, the no-tool
  streak resets, and the loop re-asks.
- Dispatched execution errors persist permanently in history (valuable
  debugging information).
- Neutral abort wording: after 3 consecutive failures, the task is ended with
  neutral language (the word "terminated" is completely removed).
- Compacts history when reaching `max_history_messages`, retaining the system
  prompt, first real user message, the compaction summary, and the trailing
  atomic tool group.
- Emits JSON events (`status`, `tool_call`, `model_message`, `compaction`,
  `error`) with quiet corrective statuses and full `raw`/`arguments` details.
  When a tool's raw return value is a dict it also travels as
  `structured_output` on the finished event, so consumers (the web server,
  the Worlds benchmark harvest) get the structured share_file metadata the
  stringified `output` display field cannot carry.
  A `skipped` parser event renders like a finished tool row.
- User interruption raises `TurnCancelled` inside the `started` event hook.
- **User message archives**: before appending the user message, `run_turn`
  hands it to `_prepare_user_message` — messages at least
  `user_message_archive_min_chars` long are archived verbatim into the pod
  by `user_messages.py` and annotated with the archive path (a failed
  archive logs and leaves the message unannotated). `_reviewer_log`
  provides the `review_work` reviewer its conversation snapshot: the
  pending review call is filtered out of the trailing assistant message
  (its sibling calls in the same batch stay; the message disappears only
  when the review call was its sole content), and the provider is
  installed as a thread-local around
  `parse_and_execute_tools` and popped on every exit path.

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
  without restricting what the model may do there.
- Per-call context (thread-locals, set inside the tool worker thread by
  `execute_tool_with_timeout`): `set_call_output_path` /
  `get_call_output_path` carry the `/tool_outputs/` path where the current
  call's full output belongs, and `set_last_exit_code` / `pop_last_exit_code`
  carry the exit code `execute_command` reports back to the result protocol.
- `next_call_index()` — monotonically increasing counter used for the
  zero-padded `/tool_outputs/NNNN_tool.txt` file names.
- `init_docker_client` / `close_docker_client` manage the global client
  lifecycle; `reset_shared_container` destroys the shared pod so the next
  `get_container()` lazily starts a fresh one (CLI `/newchat`).

### `src/syna/tools.py`
The tools exposed to the model: `think` (no-op reasoning record; produces a
`(noted)` tool result), `execute_command` (shell or argv style inside the container),
`read_file`, `edit_file` (both proxied into the pod through temp copies),
`web_search`, `web_interact`, `describe_image`, `speech_to_text` (pod audio →
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
`InteractionCancelled` when the task is stopped mid-question — the client
adds the "Other — type your own response" option itself, and an empty
choices array means an open text answer), and `review_work` (isolated
adversarial verification through `verification.py`; the reviewer's report
is the tool result). The final answer to the user is
delivered as natural language text when the model completes its work without
tool calls.

Pod artifacts are written read-only as an integrity barrier (the model has
root and can undo it; the point is that a result already reported cannot be
silently overwritten — regeneration produces new, timestamped names):
`_write_into_pod` chmods 444 every artifact it copies in (screenshots,
transcripts, audio, generated images), `execute_command`'s pod-side runner
chmods its `/tool_outputs/` capture file, and `_persist_tool_output` in
`parse.py` chmods the host-persisted outputs. Host-side logs and session
dumps are deliberately not read-only: their writers rewrite them in place
(os.replace over a read-only target fails on Windows).

Every docstring is injected verbatim into the system prompt (`build_prompt`),
so the docstrings are the model-facing tool documentation and must stay in
sync with actual behavior.

`execute_command` hardening (reliability inside the pod, not restriction —
the model keeps full root and can cd, edit system files, and install
packages): the command runs via `timeout -k 5 <command_timeout_s> bash -c`
inside the pod so hangs are killed instead of detaching; its output is
redirected to `/tool_outputs/NNNN_execute_command.txt` (file capture means
background processes started by the command cannot hold the exec stream open
and hang the harness); a per-command environment adds non-interactive
conveniences (`DEBIAN_FRONTEND=noninteractive`, pagers to `cat`,
`GIT_TERMINAL_PROMPT=0`, ...) that the model can override inline. A second
exec reads a bounded view of the output file (head-and-tail at
`max_command_output_chars` bytes) together with the recorded exit code;
exit codes 124/137 (timeout killed) map to status `timeout`, other non-zero
codes to status `error`. When no call output path is set (direct host-side
use), it falls back to a plain `container.exec`.

### `src/syna/user_messages.py`
Verbatim read-only archives of large user messages inside the pod. When a
message is at least `user_message_archive_min_chars` long,
`archive_user_message(text, container, seq)` writes it to
`<user_messages_dest>/NNNNNN.txt` (host temp file → `copy_to`, written
as bytes so text-mode newline translation cannot corrupt the verbatim
copy, then `chmod 444` — the model has root, this is an integrity
barrier, not a security boundary) and the history copy is annotated with
the archive path via `USER_MESSAGE_MARKER_TEMPLATE`; the marker carries
a per-session random archive id (generated in `session.py`), and
`ARCHIVE_MARKER_PATTERN` is what `context.py` elides against — a marker
with a foreign id is a quotation, not an archive, and is left alone. Any
failure is logged and returns None so a broken archive never blocks the
turn.

### `src/syna/verification.py`
The `review_work` reviewer session. `run_verification(summary)` runs an
isolated single-turn request — fresh client (`create_model_client`),
`assets/verifier_prompt.md` as the system prompt, **no tools** — whose
user message carries the last `verify_log_tail_chars` characters of the
agent's serialized message history (`json.dumps`, minus the assistant
message holding the pending `review_work` call itself) inside a
```` ```text ```` block labelled "Last N chars of agent execution log",
followed by the summary the agent intends to report. The reviewer model
is `VERIFIER_MODEL` (falling back to `MODEL`) at `VERIFIER_MODEL_EFFORT`
(default `high`); the reviewer's text is the tool result and nothing
else — no session id, no diagnostic dump, no UI/history artifacts beyond
the tool call and its result. The conversation snapshot arrives through
a thread-local provider (`set_conversation_log_provider` /
`current_conversation_log_provider`), propagated into tool worker
threads by `parse.py` like the container binding; failures follow the
`describe_image` convention: an `{'error': ...}` dict, never a raise.

### `src/syna/vision.py`
`describe_image(file_path, question)` — image understanding through a
dedicated single-turn vision request. Copies the image from the pod,
converts it to PNG downscaled into a 1024x1024 box (via `imaging.py`),
logs the payload size (pixels and PNG bytes, so a failing vision request
leaves data instead of a mystery), base64-encodes it into a fresh
single-message conversation (plain-text response, no JSON mode), and
replaces the bytes with a text placeholder carrying the description so
images never linger in history. The request runs at `medium` reasoning
effort: without it the provider default for this model is maximum, which
burns the completion budget on hidden reasoning and can return no content
at all.

### `src/syna/web_search.py`
`search(query, delay_s=2)` — URLs are fetched and converted to markdown via
`web_to_markdown`; everything else is searched through the stealth browser
(Camoufox) against server-rendered endpoints, trying engines in order
(DuckDuckGo html, DuckDuckGo lite, Bing, Mojeek) until one returns results,
with per-engine redirect unwrapping (DuckDuckGo's `uddg=` wrapper, Bing's
base64 `/ck/a` redirect) and an AD-loaded warm browser kept for the process
lifetime. A previous ddgs-based implementation was dropped: its plain-HTTP
engine fleet was blocked or challenged (google 403, brave 429, DuckDuckGo
202 anomaly pages, startpage bot walls), its only working engine (yahoo)
failed 60% of the time, and every failed attempt was bot-shaped traffic
that risked the IP's reputation — which would also poison the stealth
browser. Navigations carry a same-site referer, and an engine that serves
a captcha/challenge page (detected by widget selectors or marker phrases)
is paused for a randomized 15-30 minute cooldown instead of being
re-requested on the next search — repeated visits to a host that demands
verification are the repeat-offense pattern that hardens soft challenges
into IP bans. Sleeps a randomized duration in `[delay_s, 1.5*delay_s)` before
every request (fixed waits are a fingerprint of their own), holding a
process-wide lock across the sleep and the request so searches from
concurrent sessions or detached calls serialize and stay at least
`delay_s` apart instead of firing together.

### `src/syna/web_to_markdown.py`
Headless-browser page fetcher (Playwright/DrissionPage) that renders JavaScript
pages and converts the DOM to markdown.

## Concurrency model

- CLI: one thread; tools run in short-lived daemon threads created by
  `parse.py` so a hung call can be detached at the timeout. The worker thread
  inherits the container binding, the question responder, the
  `review_work` conversation-log provider, and the call's
  output path, and reports the tool-recorded exit code back to the parser.
- Web: each session has one worker thread that owns its container and runs
  turns sequentially; tools inside a turn run in per-call daemon threads that
  inherit the session's container binding and responder. Detached (timed-out)
  tool threads keep running until the call completes; since container
  commands run as real processes inside the pod, the model can terminate
  them with a follow-up `execute_command`. A pending `ask_user` question
  blocks both the tool worker and the turn thread indefinitely — by design —
  until the user answers or stop/new-chat/teardown releases the pending
  question with a cancellation sentinel.

## Tests

All suites are plain scripts runnable with the project venv
(`.venv\Scripts\python.exe tests\<file>.py`) and require no Docker:

- `tests/test_json_protocol.py` — native protocol tests: tool schema generation
  from `tools.py` via `tool_schemas.py`, argument decoding and signature validation,
  batch stop-on-failure with skipped statuses, think tool results and signatures,
  structured output events, and channel sentinel/marker escaping. Also covers the
  `structured_output` event contract: dict-returning tools carry their raw return
  value on the finished event, string-returning tools do not, and
  `session.shared_file_metadata()` matches only well-formed finished `share_file` events.
- `tests/test_editor.py` — line-editor validation: valid and combined edits
  apply; null/non-object payloads, multi-key operations, missing required keys
  and unknown operation types raise clean `ValueError`s and leave the file
  untouched.
- `tests/test_tool_feedback.py` — session-level loop: history wiping after
  success, abort after 3 consecutive failures, `<tool_results>` marker
  format, parser warnings surfaced as harness notices, think+respond turns,
  step-budget warning, final status report delivered at exhaustion (and the
  error path when the final reply holds no valid `respond`), repeated-call
  nudges, compaction channel detection, and web event details.
- `tests/test_output_and_persistence.py` — `output.py` cleaning and
  head-and-tail rendering, plus `/tool_outputs/` persistence using a fake
  container (host tools persisted; pod-native `execute_command` not;
  failures degrade gracefully without `saved_to`).
- `tests/test_execute_command_scripts.py` — runs the exact pod-side
  runner/reader shell scripts generated by `execute_command` through a local
  real bash (Git Bash on Windows): exit-code capture, `(no output)`,
  head-and-tail marker, and timeout kill with partial output. Does not
  exercise bollard/Docker itself.
- `tests/test_request_payload.py` — verifies the request payload sent to
  OpenRouter carries the system prompt with real quote characters (no
  escaping-layer artifacts) and survives a JSON roundtrip unchanged.
- `tests/test_context_management.py` — token estimation, elision semantics
  (saved_to-only, recency window, idempotence), pod `HostConfig` assembly,
  a session-level elision-under-budget run, interval elision with per-request
  batching disabled, and a session-level proof that outputs stay full-text
  between batched elision passes.
- `tests/test_eval_scoring.py` — eval outcome scoring: honest failures,
  false successes, false failures, clarifying questions, no-report runs,
  and the headline rate computation.
- `tests/test_simulated_task.py` — an end-to-end scripted task: batched
  calls with a failure and a skip, think+retry, persistence, elision under
  the token budget, and a clean respond.
- `tests/test_ask_user.py` — the interaction bridge and tool: schema
  registration, responder round-trip and thread propagation into tool
  workers, no-responder and malformed-argument errors, cancellation
  (batch stops, remainder skipped), the `None`-timeout exemption (a slow
  question is not detached), `PendingQuestion` resolve/release semantics,
  and the terminal responder's number-or-text answer handling.
- `tests/test_user_message_archives.py` — the archive tool and session  hook: read-only pod write (exact bytes including LF newline
  preservation, mkdir + chmod 444 commands),
  failure returning None, large messages archived and annotated in
  `run_turn` (small and channel messages untouched, failed archives leave
  the turn unannotated but working, per-session archive ids differ), and
  the elision pass (head excerpt
  plus pointer, recency window, idempotence, plain/assistant messages
  ignored, markers with foreign archive ids rejected).
- `tests/test_review_work.py` — the verification tool: reviewer request
  shape (verifier prompt asset verbatim, labeled code-block log tail with
  truncation at `verify_log_tail_chars`, summary included, no tool
  schemas, no session id), model/effort selection with fallback and
  validation, no-provider/failed-reviewer/missing-model error dicts,
  schema registration, the `None`-timeout exemption in `parse.py`, the
  reviewer log filtering only the pending `review_work` call (siblings
  survive; a completed earlier review call stays), and a `run_turn`
  end-to-end run where history gains only the call, its result, and the
  final answer, with the provider popped afterward.
- `tests/test_paths_and_setup.py` — the filesystem layout and the setup
  wizard: bundled-asset resolution, `SYNA_HOME` override with first-run
  seeding, repo-root fallback, the endpoint-needs-key matrix, the
  `setup_incomplete` matrix (model/key/engine), `.env` merging with
  cache clearing, provider preset integrity, detection returning data
  instead of raising, install failures returned as data, the wizard
  page served (and left) based on live setup state, and the setup API
  endpoints (token-gated detect, unknown dependency, key/validation
  errors on config).
- `tests/test_image_endpoint.py` — the inline image endpoint against a
  fake pod: path validation (outside `/workspace`, traversal, non-image
  extensions), pass-through for small in-box images (animated GIF bytes
  untouched), downscaling of oversized dimensions into the 1024x1024 PNG
  box, the 50 MB rejection, the recompression threshold, malformed-image
  rejection, and the LRU cache eviction.
- `tests/test_net_guard.py` — the browser's network policy: blocked-range
  matrix (loopback v4/v6/mapped, RFC1918, metadata, CGNAT, multicast),
  URL-spec obfuscations (decimal, hex, octal, mixed, trailing dot, and
  shortened dotted forms with large last labels — asserted against the
  correct ranges, not 0/8 luck), public short forms not blocked, hostname resolution blocked/allowed
  with stubbed `getaddrinfo`, resolution-failure pass-through, cache
  behavior, config overrides, scheme handling (ws/wss guarded, data:
  passed), `tool_goto` blocking before any page exists, and the route
  handler's neutralize/continue/abort outcomes plus the WebSocket
  observer.
- `tests/test_server_auth.py` — the local-origin and token enforcement:
  open API without a token, 401 without / 404 with the token (header and
  query), 403 on foreign Host headers, localhost accepted, shell and
  vendor staying open with the token injected, and WebSocket handshake
  rejection for foreign Origins and missing tokens.
- `tests/test_web_search_pacing.py` — search rate-limit pacing: concurrent
  searches never overlap, the gap survives separate non-overlapping calls,
  and the pacing sleep is randomized within `[delay_s, 1.5*delay_s)` rather
  than a fixed value.
- `tests/test_web_search_parsing.py` — search result post-processing:
  DuckDuckGo `uddg=` and Bing base64 `/ck/a` redirect unwrapping (including
  plain links and malformed payloads), the title/URL/snippet block
  renderer, challenge-page detection (widget/marker probes), and the
  engine cooldown: challenged engines are paused and skipped, healthy
  engines behind a broken one keep serving.
- `tests/test_live_web_search.py` — live search chain (manual run): every
  extractor verified against markup fixtures in the real browser, live
  ddg-html/ddg-lite/bing required to return results (mojeek best-effort —
  it may serve an ALTCHA challenge, which the chain treats as no
  results), broken-first-engine fall-through, and graceful degradation
  when every engine is broken.
- `tests/_runner.py` — shared harness for the plain-script suites: a
  monkeypatch (attribute/mapping overrides with automatic undo) and
  `run_suite`, which executes every test, prints per-test results, and
  exits non-zero on failure.
- `evals/` — the live honesty eval suite (see `evals/README.md`): impossible
  and artifact tasks run against a real pod, scored mechanically, with the
  false-success rate tracked in `evals/results/history.jsonl`. Tasks include
  `impossible-visual-defect`, which plants a PDF whose table header row is
  shifted one column against its data — the defect observed in the first
  live Worlds run — and checks the agent renders the page (or admits it
  could not verify the layout) instead of calling the deliverable ready
  from a content-only check.
- `evals/run_worlds_benchmark.py` — THE LEAGUE OF LEGENDS WORLDS
  BENCHMARK. NOT a regular test and deliberately excluded from every
  run-all loop: it is not in `tests/` (no test runner discovers it) and
  not registered in `evals/tasks.py` (`run_evals.py` can never pick it
  up). Run it ONLY on significant project changes or on explicit user
  request: one live run takes 30-45 minutes, hundreds of model steps,
  and a fresh pod. It gives the agent one long multi-step task (a
  League of Legends Worlds PDF report) whose facts are scattered across
  many sites and API calls, saves the full transcript, the delivered
  artifacts (`shared/`), and this run's log slice to
  `evals/results/worlds_<timestamp>/`, and scores nothing — analysis is
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
- `tests/test_browser_health.py` — live browser health checks (manual run).
- `tests/test_live_truthfulness.py` — live model evaluation scenarios
  (manual run, requires a container runtime and an API key).
