# Syna — Technical Description

## Overview

Syna is a terminal and web AI agent that answers user requests by driving an
LLM (through OpenRouter) in a structured tool-call loop. Under the Syna v5
protocol, the model communicates strictly through JSON: every response must
be a non-empty array of tool call objects `{"tool_name": {...args}}` (a bare
single object is tolerated, as is one surrounding code fence or a leading
`<think>` block). The parser decodes only the FIRST JSON value and discards
any surrounding text, reporting it back to the model as a warning, so a
fabricated tail can never execute. The agent executes the requested tools
sequentially — the first failing call stops the batch and the remaining calls
are reported as `skipped` — until the model calls `respond`, whose text is
delivered to the user as the final answer. `respond` may share a reply with a
preceding `think` call only.

Tool results reach the model as `<tool_results>` blocks containing one
`<result call=... tool=... status=... exit_code=... saved_to=...>` element per
call, with head-and-tail truncated output; the full output of every call is
saved inside the pod at `/tool_outputs/` and its path is the `saved_to`
attribute. Harness-originated messages (corrective warnings, format errors,
step-budget warnings, history summaries) arrive wrapped in `<harness_notice>`
tags. All system-originated content is additionally framed by sentinel
channels (see `channel.py`) so the model cannot forge it.

Most tools execute inside a per-session Docker/Podman container (image
`python:3.14-trixie`, managed through the `bollard` client), giving the model
a sandboxed Debian machine ("the pod") in which it can run commands and
create files with full root access.

## Running

- CLI: `syna` (entry point `syna:main`) — interactive prompt `/ask Syna):`,
  supports slash commands.
- Web server: `syna-server` (entry point `syna.server:main`) — FastAPI +
  WebSocket chat UI served from `assets/chat.html`.
- Web UI sources live in `build/` (see below); after editing them, regenerate
  the UI with `python scripts/build.py` — `assets/chat.html` is a generated file and
  must not be edited directly.
- Install with `uv sync`; Python >= 3.12 required. Copy `.env` with
  `OPENROUTER_API_KEY` before starting.

## Dependencies

- `openrouter` — model client with configurable timeout/retry.
- `python-dotenv` — loads `OPENROUTER_API_KEY` and model settings from `.env`.
- `httpx` — OpenRouter audio endpoints (speech transcription and synthesis).
- `bollard` — Docker/Podman container client used for the sandbox.
- `ddgs`, `playwright`, `drissionpage`, `camoufox`, `markdownify` — web search
  and page-to-markdown conversion, plus the persistent stealth browser session.
- `pillow` — image downscaling for the vision pipeline.
- `fastapi`, `uvicorn`, `python-multipart` — web server and uploads.

## Configuration

`.env` holds the secrets and model selection:

| Key | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | API key for every OpenRouter request. |
| `MODEL` | Main agent model. |
| `STT_MODEL` | Voice transcription model (voice dictation off when unset). |
| `TTS_MODEL` | Speech synthesis model (reply read-aloud off when unset). |
| `TTS_VOICE` | Synthesis voice (default `Kore`). |
| `TTS_RESPONSE_FORMAT` | Speech endpoint response format (default `pcm`; raw samples are wrapped in a WAV container server-side). Some models, e.g. OpenAI TTS, also accept `mp3`. |
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
| `max_steps_per_turn` | Model requests allowed per turn (default 100). Before the cap, the model receives a wrap-up warning; after it, the turn ends. |
| `step_warning_remaining` | Remaining-steps count that triggers the wrap-up warning (default 20). |
| `context_budget_tokens` | Estimated-token budget for the whole conversation (default 100000). When exceeded, old tool outputs are elided first and summarization runs only if the budget is still exceeded. |
| `elide_keep_recent_messages` | History messages kept untouched by output elision (default 12). |
| `elide_interval_messages` | History size at which lossless output elision runs on its own, instead of waiting for the token budget (default 30). Keeps prompts small through long runs; elided outputs remain readable in the pod at their `saved_to` paths. |
| `max_history_messages` | History length that triggers compaction. |
| `enable_compaction` | Master switch for history compaction. |
| `max_visible_tool_calls` | UI limit on rendered tool rows. |
| `log_file` | Log file name (written to the project root). |
| `session_dump_file` | Per-session message dump file name template. |
| `server_host`, `server_port` | Web server binding. |
| `session_idle_timeout_min` | Idle web sessions are reaped after this. |
| `upload_dest` | Default upload destination inside the pod. |
| `voice.silence_timeout_ms` | Mic silence that ends a voice recording. |
| `voice.silence_threshold_dbfs` | Loudness level treated as silence. |
| `voice.max_recording_ms` | Hard cap on one voice recording. |
| `voice.voice_mode_on` | Initial voice-mode state pushed to the client on every fresh session (page load, new chat, server restart). Accepts booleans and "true"/"false" strings; voice never persists across sessions. |

Static assets live in `assets/`: `prompt.md` (system prompt template with a
`{tools}` placeholder where live tool documentation is injected, plus the
`{{CURRENT_DATE}}`, `{{COMMAND_TIMEOUT}}` and `{{OUTPUT_LIMIT}}` placeholders
filled at build time), `compaction_prompt.md`, `chat.html` (web UI) and
`vendor/` (markdown-it, KaTeX, fonts, icons, logo).

## Web UI build system

`assets/chat.html` is GENERATED by `scripts/build.py` — never edit it directly. The
sources live in `build/`:

- `build/index.html` — page skeleton with `{{CSS}}` and `{{JS}}` markers.
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
- Tool-row headers read the parsed `arguments` of the event (per-tool
  primary key) instead of regex-parsing the repr signature, which broke
  on commands containing single quotes (Python repr switches to double
  quotes and the first single-quoted span won — e.g. heredoc commands
  showing "EOF"). The signature parser remains only as a replay fallback.

## File structure and module responsibilities

### `src/syna/__init__.py`
CLI entry point. `main()` starts the ask loop, handles Ctrl+C, and closes the
global Docker client on exit.

### `src/syna/ask.py`
CLI loop. Renders session events (`_print_event`), dispatches slash commands
via `commands.parse_and_execute_command`, and feeds plain queries to
`AgentSession.run_turn`.

### `src/syna/browser.py`
Persistent stealth browser session (Camoufox) exposed as LLM-callable actions.
Renders pages through an accessibility-style walker that emits a compact
line-based view (URL, title, scroll position and numbered interactables), and
dispatches commands (`goto`, `click`, `type`, ...) by numeric element ref.

Hardening: hosts whose ad/identity scripts fail under uBlock (which makes
Fandom wipe the page with an error-report modal) are fulfilled with empty
JavaScript (`NEUTRALIZED_SCRIPT_PATTERNS`); popups are only adopted as the
current page right after a user-initiated interaction, unsolicited ones are
closed; renderer crashes are logged and the page transparently reloaded
(`_recover_from_crash`); `read` retries once and returns a diagnostic instead
of an empty string when the document is blank; page errors, JS errors and
failed requests are logged to the dev log.

### `tests/test_browser_health.py`
Live browser health checks (run manually): Phase 1 reproduces the
goto-read-soak workflow against a heavy Fandom page and reports crashes,
popup hijacks, navigations away, or blank reads over 90 seconds; Phase 2
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
session_id)` — sends model requests with configurable delay, request dumps,
and structured `response_format: {"type": "json_object"}`. Every request
carries `max_tokens` from the `max_output_tokens` config value (default
16384), so reasoning models cannot starve the visible reply of the
provider's default completion budget; `finish_reason` and the
prompt/completion/reasoning/cached token split are logged for every
response. When a `session_id` is given it is sent with every request so
OpenRouter pins provider sticky routing and the provider-side prompt cache
stays warm for the whole conversation. Accepts an optional `accept_reply`
validator that raises `ReplyRejected` on channel impersonation. Transient
empty responses (no content) are retried up to `EMPTY_RESPONSE_MAX_ATTEMPTS`
times.

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
Token estimation and tool-output elision for context management:
`estimate_tokens` / `estimate_history_tokens` (characters/4 heuristic, no
tokenizer dependency), and `elide_tool_outputs` — replaces old result
outputs that carry a `saved_to` path with a one-line pointer to the pod
file, leaving the system message, the most recent messages, and anything
unrecoverable untouched. Idempotent.

### `src/syna/config.py`
Configuration and logging: `load_config()` (reads `assets/config.json`),
`get_logger()` (file logger), `get_project_root()`, and loaders for the
protocol limits the prompt and tools consume: `load_output_limit_chars()`
(head-and-tail cut), `load_command_timeout_s()` and
`load_tool_call_timeout_s()`.

### `src/syna/editor.py`
Read/edit primitives used by the `read_file` and `edit_file` tools. Edit
operations are validated before the file is touched: non-dict or multi-key
operations, non-object payloads, missing required keys, unknown operation
types, out-of-bounds line numbers, invalid ranges and overlapping ranges all
raise clean `ValueError`s instead of leaking internal TypeErrors.

### `src/syna/output.py`
Cleaning and bounded rendering of tool output shown to the model:
`strip_ansi_escapes`, `collapse_progress_lines` (carriage-return progress
updates reduce to their last segment), `head_and_tail` (keeps the beginning
and end of long output with an explicit omission marker pointing at the
`saved_to` file), and `bounded_output` (cleaning plus cut plus an explicit
`(no output)` placeholder for empty results).

### `src/syna/inspect_module.py`
Extracts function names, docstrings and argument metadata via `ast`; used to
inject live tool documentation into the system prompt and to validate tool
call arguments in `parse.py`. Each argument carries its name, type
annotation, whether it has a default, and the default's source text.

### `src/syna/parse.py`
Tool-call parsing and execution under the v5 pure-JSON protocol.

- `build_signature(tool_name, tool_arguments)` — one-line python-like
  rendering of a call.
- `load_tool_call_timeout()` — configured `tool_call_timeout_s` (default 60).
- `_decode_reply(text)` — strips an optional UTF-8 BOM and one leading
  `<think>...</think>` block and one complete surrounding code fence, then
  decodes only the FIRST JSON value with `JSONDecoder(strict=False)
  .raw_decode` (literal newlines inside strings are accepted). Text before or
  after the value is discarded and returned as warnings; a reply with no
  decodable JSON raises `ReplyFormatError` and nothing is executed.
- `_extract_tool_calls(data)` — validates JSON shape (single object with one
  key or non-empty array of single-key objects; arguments must be a dict).
  One leniency: a multi-key object that contains `think` is accepted and
  split into the sequential calls it obviously means (think first), since
  models frequently write `{"think": ..., "tool": ...}` against the one-key
  rule; multi-key objects without `think` stay rejected.
- `_enforce_respond_placement(calls)` — `respond` may appear once and only
  beside `think` calls; anything else rejects the whole reply.
- `ParseOutcome(structural_error, outputs, warnings)` — returns
  `structural_error` if the reply fails formatting, shape, or respond
  placement, in which case nothing dispatches. `outputs` carries one entry
  per processed call, including skipped ones; entries for calls that never
  reached dispatch carry `"error": True` and `"status": "error"`. Executed
  entries carry `"call"` (position in the model's array), `"status"`
  (`ok` / `error` / `timeout` / `skipped`), `"exit_code"` (from
  `execute_command`), and `"saved_to"` (pod path with the full output).
- Sequential execution with stop-on-first-failure: a bad signature, raised
  exception, timeout, or non-zero command exit marks the rest of the batch
  `skipped`. `think` runs as a no-op and produces no result entry, but its
  signature is reported back via `ParseOutcome.think_signatures`.
- Browser command failures (`web_interact` views starting with "ERROR:")
  record exit code 1, so they surface as `error` rows and stop the batch;
  `read_file`/`edit_file` translate the Docker 404 for an absent pod path
  into a clean "No such file or directory" error with exit code 1, while
  other Docker errors propagate unchanged.
- `execute_tool_with_timeout(tool_name, tool_arguments, timeout_s,
  output_path)` — runs the tool in a daemon worker thread, propagating the
  calling thread's registered container and the call's output path into the
  worker (both are thread-locals), and reports back the tool-recorded exit
  code. On timeout the worker is detached and a notice is returned as the
  tool output with status `timeout`.
- `_persist_tool_output(output_text, saved_to, persist_outputs)` — saves
  host-side tool output (web tools, file tools, ...) into the pod at
  `/tool_outputs/` via `copy_to`; `execute_command` writes its own file
  pod-side instead. Web sessions persist through the thread's bound
  container; CLI sessions opt in via `persist_outputs` and use the shared
  pod. When no container can be used (unit tests, persistence errors) the
  output is still delivered — only the `saved_to` attribute is dropped.
- Output shown to the model is cleaned (ANSI stripped, progress lines
  collapsed, empty → `(no output)`) and cut to head-and-tail at
  `max_command_output_chars`.
- `parse_and_execute_tools(text, on_tool_event, echo_errors,
  tool_timeout_s)` — validates payload, shape, and respond placement; emits
  `started` / `finished` / `error` / `skipped` events with the exact `raw`
  JSON and parsed `arguments` for front-end call details; `failed=True` is
  passed on finished events for failed runs so the UI can style them.

### `src/syna/server.py`
FastAPI app. One `SessionRecord` per client id owns an `AgentSession`, a
dedicated container (registered thread-locally in the record's worker thread),
event queues, an event log for WebSocket replay, and shared-file downloads
(when a finished `share_file` event carries `structured_output` metadata —
matched via `session.shared_file_metadata()` — the record copies the pod path
to the host and pushes a `file` event the client renders as a download;
worker teardown releases the container and model client; `share_file`
recreates the session temp dir if it was removed externally). A janitor
coroutine reaps idle, disconnected sessions. `index()` serves `chat.html` and
injects `window.SYNA_CONFIG` (UI limits plus voice availability, tuning and
the configured initial voice-mode state).
Speech endpoints: `POST /api/speech/transcribe` (multipart clip to text) and
`POST /api/speech/synthesize` (JSON text to audio in the configured format;
'pcm' replies are wrapped in a WAV container server-side). `main()` runs
uvicorn.

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
to an audio format; `client_configuration` collects the settings the web
client needs for voice mode. Failures raise `SpeechError` with a user-safe
message.

### `src/syna/session.py`
`AgentSession` — the shared conversation loop for CLI and web.

- Builds the system prompt from `assets/prompt.md`: injects the live tool
  documentation (docstrings from `tools.py`) at `{tools}` and fills
  `{{CURRENT_DATE}}`, `{{COMMAND_TIMEOUT}}` and `{{OUTPUT_LIMIT}}`.
- `run_turn(query)` requests model responses and executes tools via
  `parse_and_execute_tools`.
- **Self-cleaning history**: malformed tool attempts and their corrective
  `HARNESS_NOTICE` warnings remain in history during retries (streak counter
  capped at `MAX_NO_TOOL_RETRIES = 3`). The moment a subsequent reply
  successfully forms and executes a tool call (or `respond`), all preceding
  consecutive failed attempts and notices are spliced out of `self.messages`,
  preventing the model from replicating its mistakes.
- **Sentinel channel separation**: tool results are appended as
  `wrap_channel("TOOL_RESULTS", build_tool_results_body(...))` — a
  `<tool_results>` block with one labeled `<result>` per call plus parser
  warnings as `<harness_notice>` — corrective warnings, file uploads,
  step-budget warnings and interruptions as `HARNESS_NOTICE`, and compaction
  summaries as `COMPACTION`.
- **Two-stage context management** (`context.py`): once the history reaches
  `elide_interval_messages`, and additionally whenever estimated tokens
  exceed `context_budget_tokens`, old tool outputs (those with `saved_to`)
  outside the recent window are elided to pointers — lossless, since the
  model can re-read them in the pod. Running elision on its own message
  interval keeps prompts small through a long run instead of letting the
  history balloon until the budget forces a lossy summarization.
  Summarization runs only if the message count reaches
  `max_history_messages` or the budget is still exceeded after elision,
  keeping the system prompt, first real user message, the compaction
  summary, and the newest message.
- **Step budget**: each turn allows `max_steps_per_turn` model requests; when
  `step_warning_remaining` steps are left, the model receives a wrap-up
  notice; past the cap the turn ends with an explanatory notice.
- **Loop detection**: identical non-`think`/non-`respond` calls repeated
  `REPEATED_CALL_LIMIT` (3) times within one turn trigger a one-time
  "change approach" harness notice; the same guard covers identical think
  notes (lone or batched).
- **Lone-think acceptance**: a reply containing only `think` calls is
  accepted rather than bounced — the reply persists in history (honoring the
  think tool's promise that entries stay visible), pending corrective
  notices from earlier malformed attempts are purged, the no-tool streak
  resets, and the loop re-asks. Discarded text beside the think calls earns
  a one-time corrective warning, cleaned up by the next success. The prompt
  still steers the model toward pairing think with the action or respond it
  informs (one request instead of two).
- Dispatched execution errors persist permanently in history (valuable
  debugging information).
- Neutral abort wording: after 3 consecutive failures, the task is ended with
  neutral language (the word "terminated" is completely removed).
- Compacts history when reaching `max_history_messages`, retaining the system
  prompt, first real user message, the compaction summary, and the newest
  message.
- Emits JSON events (`status`, `tool_call`, `model_message`, `compaction`,
  `error`) with quiet corrective statuses and full `raw`/`arguments` details.
  When a tool's raw return value is a dict it also travels as
  `structured_output` on the finished event, so consumers (the web server,
  the Worlds benchmark harvest) get the structured share_file metadata the
  stringified `output` display field cannot carry.
  A `skipped` parser event renders like a finished tool row.
- User interruption raises `TurnCancelled` inside the `started` event hook.

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
  lifecycle.

### `src/syna/tools.py`
The tools exposed to the model: `think` (no-op reasoning record; produces no
result), `execute_command` (shell or argv style inside the container),
`read_file`, `edit_file` (both proxied into the pod through temp copies),
`web_search`, `web_interact`, `describe_image`, `share_file` (registers a pod
path for user download; a missing path is reported as a normal error row
with exit code 1, and the delivery metadata reaches the client through the
`structured_output` event field), and `respond` (final answer to the user).

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

### `src/syna/vision.py`
`describe_image(file_path, question)` — image understanding through a
dedicated single-turn vision request. Copies the image from the pod, converts
it to PNG, downscales it to fit a 1024x1024 box with Pillow, base64-encodes
it into a fresh single-message conversation (plain-text response, no JSON
mode), and replaces the bytes with a text placeholder carrying the
description so images never linger in history.

### `src/syna/web_search.py`
`search(query)` — DuckDuckGo text search for queries; URLs are fetched and
converted to markdown via `web_to_markdown`.

### `src/syna/web_to_markdown.py`
Headless-browser page fetcher (Playwright/DrissionPage) that renders JavaScript
pages and converts the DOM to markdown.

## Concurrency model

- CLI: one thread; tools run in short-lived daemon threads created by
  `parse.py` so a hung call can be detached at the timeout. The worker thread
  inherits the container binding and the call's output path, and reports the
  tool-recorded exit code back to the parser.
- Web: each session has one worker thread that owns its container and runs
  turns sequentially; tools inside a turn run in per-call daemon threads that
  inherit the session's container binding. Detached (timed-out) tool threads
  keep running until the call completes; since container commands run as real
  processes inside the pod, the model can terminate them with a follow-up
  `execute_command`.

## Tests

All suites are plain scripts runnable with the project venv
(`.venv\Scripts\python.exe tests\<file>.py`) and require no Docker:

- `tests/test_json_protocol.py` — exhaustive protocol tests: decoding
  (BOM, think blocks, fences, first-JSON-value-wins with warnings), shape and
  signature validation, respond/think placement, batch stop-on-failure with
  skipped statuses, think-only replies, and channel sentinel/marker escaping.
  Also covers the `structured_output` event contract: dict-returning tools
  carry their raw return value on the finished event, string-returning tools
  do not, and `session.shared_file_metadata()` matches only well-formed
  finished `share_file` events.
- `tests/test_editor.py` — line-editor validation: valid and combined edits
  apply; null/non-object payloads, multi-key operations, missing required keys
  and unknown operation types raise clean `ValueError`s and leave the file
  untouched.
- `tests/test_tool_feedback.py` — session-level loop: history wiping after
  success, abort after 3 consecutive failures, `<tool_results>` marker
  format, parser warnings surfaced as harness notices, think+respond turns,
  step-budget warning and exhaustion, repeated-call nudges, compaction
  channel detection, and web event details.
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
  and a session-level elision-under-budget run.
- `tests/test_eval_scoring.py` — eval outcome scoring: honest failures,
  false successes, false failures, clarifying questions, no-report runs,
  and the headline rate computation.
- `tests/test_simulated_task.py` — an end-to-end scripted task: batched
  calls with a failure and a skip, think+retry, persistence, elision under
  the token budget, and a clean respond.
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
