# Improvement Backlog

Researched improvements that have potential but are not implemented. Each
entry records the evidence, the fit to Syna, and what blocks it. Do not
implement an entry without re-checking the research: the field moves fast,
and the numbers below have expiry dates.

## Sub-agent architecture (context isolation for exploration)

Spawn a fresh, single-purpose agent session for exploratory work (codebase
study, web research, data profiling) and return only a condensed summary to
the main agent. The mechanism is compression: a sub-agent may burn tens of
thousands of tokens exploring and return 1-2k.

Evidence:

- Anthropic, "How we built our multi-agent research system" (2025-06):
  substantial improvement over single-agent systems on complex research
  tasks; sub-agents explore in parallel with clean context windows.
- Anthropic, "Effective context engineering for AI agents" (2025-09):
  sub-agent architectures named one of the three main context levers,
  alongside compaction and structured note-taking.
- OpenCode ships a dedicated read-only "explore" agent for the same
  reason: search floods context, and delegation keeps the main window
  focused on synthesis.

Fit to Syna: Syna already runs the minimal version of this pattern - the
adversarial work reviewer receives a bounded log tail in a clean context.
Generalizing it into a `delegate` tool is the natural next step. Syna's
existing protections (elision, tool outputs as files, compaction) pursue
the same goal with different means, which is why this is backlog rather
than roadmap.

Prerequisites and design notes:

- Reuse the endpoint-family routing keys (session_id / prompt_cache_key)
  so a sub-agent shares or deliberately splits the prompt cache.
- The first sub-agent should be read-only (no pod mutations, no network
  writes): it bounds the blast radius of delegation mistakes and mirrors
  the explore-agent pattern.
- Cost accounting: a delegated sub-agent's token spend must surface in the
  session's own usage summary, or long sessions get opaque bills.
- Lifecycle: a delegate must respect the session's cancel event and step
  budget, or a cancelled turn can keep burning tokens in the background.

## Configurable pod permissions (network, privileges) — study if worth it

Per-session policy for what the pod may do: network egress, reachability
of the host and engine bridge, and privilege level inside the container.
The word "permissions" is deliberate — this is about what the sandbox
may touch, not what the model may say.

The problem, precisely: the pod currently runs as root with unrestricted
network access. The only ceilings are resource-shaped (`pod_memory_mb`,
`pod_pids_limit`), which bound runaway processes but not intent. The
host-side browser, by contrast, already runs behind a full network
policy engine (`net_guard`: private ranges blocked, hostname resolution
checked at decision time, allowlist config, tested matrix in
`test_net_guard.py`). The asymmetry is real and documented — the server
section of the technical description lists pod egress as an accepted
residual risk and calls it "a separate topic". This entry is that
separate topic.

Why it is not a obvious yes: the pod's most common legitimate work —
`pip install`, `apt-get`, `git clone`, `curl` — needs network, and much
of it needs root. A permissions model that breaks package installation
breaks the agent. The study has to answer whether the safety gain
outweighs the workflow cost, hence "study if worth it" rather than
roadmap.

The design space to evaluate:

- **Egress modes**, coarse to fine: off / public-only (reuse
  `net_guard`'s blocked-range matrix — it already exists, is tested, and
  is pod-agnostic by design) / per-destination allowlist. The reuse path
  is the cheap one: the policy engine is written against destinations,
  not against the browser.
- **Enforcement point.** Engine-level (a custom podman network whose
  default route is blocked or NATed through a filter) is tamper-proof
  but per-session and heavier to set up; in-pod nftables is cheap but
  the model is root and can undo it — the docs already draw the
  integrity-vs-boundary line for read-only artifacts, and the same
  reasoning applies: in-pod rules are an integrity barrier, not a
  security boundary. DNS-level filtering (block resolution of private
  names) is a middle ground that mirrors `net_guard`'s hostname check.
- **Privilege shaping.** Unprivileged user, `CapDrop`, read-only
  rootfs variants. Evaluate what actually breaks: `apt-get install`
  needs root; most build work does not. A `root_required` escape hatch
  (per-call, user-approved) may be worth more than a global downgrade.
- **Approval UX.** The `ask_user` interaction bridge is the natural
  consent point: "Syna wants to download from pypi.org — allow?" gives
  the user a real decision instead of a config file they set once and
  forget. Study whether per-destination prompts are annoying enough to
  defeat the feature.
- **Denial ergonomics.** Whatever is denied must come back to the model
  as descriptive result text (the `ERROR:` convention), naming what was
  blocked and how to ask for it — otherwise the model retries and
  burns steps.

Prerequisites: decide per-session vs per-category granularity before
any code; measure whether engine-level network setup per session is
fast enough to keep session start snappy; check what Podman rootless
machines already isolate (the podman VM already blocks some host
reachability — verify before building what it gives for free).

## `wait` tool (sleep, wait until a time, wait for a job) — study if worth it

A first-class waiting verb. Today, waiting is modeled as `sleep N`
inside `execute_command`, which caps at the command timeout (50 s
default, 25 in Allegro), and anything longer must become
`nohup ... &` plus repeated polling calls — every poll costing a full
model request, a step, and a request delay. Long builds, test suites,
downloads, and "check back at 14:00" have no first-class tool.

Proposed surface, three modes:

1. `wait(seconds=N)` — sleep for N seconds.
2. `wait(until="hh:mm:ss")` — sleep until the next occurrence of that
   wall-clock time. The docstring must pin down which clock (pod-local
   vs UTC) and midnight wrap; the pod's clock is the model's world, so
   pod-local is the likely answer.
3. `wait(job=...)` — block until a pod process or file condition
   completes (the handle question below).

Budget model: the tool overrides `tool_call_timeout_s` and
`command_timeout_s` enforcement and is bounded by a new
`max_waiting_time` config instead. Mechanically this is a per-tool
custom timeout: `parse.py` already dispatches `ask_user` and
`review_work` with a `None` timeout; `wait` generalizes that to "this
tool's timeout is its own argument, capped by config". Modes 1 and 2
pre-validate: a requested wait longer than `max_waiting_time` fails
immediately with a descriptive error naming the cap and suggesting
chunked waits or backgrounding — no execution, no wasted blocking. Mode
3 blocks until completion or `max_waiting_time`; on elapse it returns
the observed state (process still alive, output tail so far) as a
normal result rather than an error, so the model decides what to do
next.

**Open question 1 (blocking): how to get a handle for a job and detect
its end.** Candidates, ranked by reliability-per-complexity:

- `tail --pid=<pid> -f /dev/null` as the blocking exec: a single
  pod-side call that exits exactly when the process dies. No host-side
  polling loop, no new session state, and the PID comes from the model
  itself — `execute_command` output can echo `$!` of a backgrounded
  job, which the prompt's `nohup` convention already teaches. This is
  the recommended v1 mechanism: one docstring sentence explains it.
- Sentinel file: the command touches a done-file when finished; `wait`
  blocks on it (inotify or a bounded poll). Handles non-PID cases
  (remote jobs that only produce a file).
- A session-level background-job registry (`execute_command` gains
  `background=true` and returns a handle). Most ergonomic, most
  plumbing — only worth it if the PID convention proves flaky in
  practice.

**Open question 2 (blocking): should it run "in parallel"?** The
fire-and-forget UX — "the command is running, go do whatever you need
in the meantime, you'll get a response when it finishes" — collides
with the closed tool-call group invariant: every `tool_call_id` must
receive a result before the next model request, so a tool cannot
"finish later". The honest options, in order of cost:

- **Blocking wait** within `max_waiting_time`: trivially correct, fits
  the current loop unchanged. The UI already renders a running tool row
  for the duration, so "Waiting for build..." is free visibility.
- **Return-immediately status**: the tool returns "job N is running;
  continue working and re-check later", and the model polls with short
  waits. Nearly free to build today, but it reintroduces the polling
  steps this tool exists to remove — measure whether it is a feature
  or a regression.
- **True async callback** (completion arrives as a harness notice via
  the queued-message injection checkpoint): the real version of the
  imagined UX, but it is a protocol change with history-integrity
  implications. Treat as its own project; only pursue if step-burn from
  the first two options shows up in practice (the Worlds benchmark's
  long jobs are the natural measurement ground).

Fit notes: Allegro should not halve waiting budgets — the wait length
is user/model intent, not a speed knob to tune. The study should start
by instrumenting how often real sessions do sleep-and-poll today; if
the answer is "rarely", the tool is a nice-to-have, not a need.

## Allegro (the Turbo switch) — implemented

Originally researched as a "Turbo switch": a client-side toggle that
appends one rule to the end of the system prompt, nudging the agent
toward the minimum number of steps. Shipped as **Allegro**.

The message the mode appends:

> You are currently operating in 'Allegro' mode. Speed is important for this task. Try to complete it in the minimum number of tool calls possible without compromising the reliability of your results. Batch tool calls when reasonable, skip optional polish, and keep reasoning, narration, and the final report short. Verify when errors would be costly or hard to notice, but don't report something as verified unless you verified it. Speed doesn't override your other values or guidelines. 

Mechanism as built:

- The composer gains a pill (`queue_music` icon + label, left of the
  send button) that toggles the mode; the state is remembered in
  localStorage. The pill collapses to an icon-only circle below 640px.
- The flag travels with each message (`{type: "message", allegro}`) and
  the server captures it at enqueue time; the worker applies it when the
  turn starts, never mid-turn. The pill is disabled while busy, matching
  the server-side guarantee.
- `build_prompt(allegro=True)` appends the rule under its own
  `## Allegro mode` h2 section; `AgentSession.set_allegro` rewrites the
  system message only on an actual change. Cost: one prompt-cache
  invalidation per real mode change, none for a no-op toggle.
- The mode also runs a lighter runtime profile, applied with the same
  turn-start swap: thinking effort becomes the faster of (user setting,
  low; OpenRouter only — custom endpoints and faster settings win), the
  per-request pause halves (2s → 1s), the step budget halves with its
  wrap-up warning clamped along, and command/tool-call timeouts halve
  through a thread-local scale that lives exactly one turn on the
  worker thread. The settings modal documents the profile read-only
  with live numbers.
- Toggle feedback follows project conventions: an ascending pentatonic
  figure on enable, its inversion on disable (Sfx catalog, `ui`
  category), and the shared `icon-pop` keyframes on the icon.

## Explicitly out of scope by design

### Cross-session persistent memory

Deliberately excluded as a privacy and safety feature. Memory that
survives a session introduces unpredictability and non-determinism into
what the agent carries into future work, and this is a cost the project
refuses on purpose. Within a single session, the `think` tool and the
notes/plan convention (`/workspace/notes/progress.md`) already cover the
scope persistent memory would serve. Do not "fix" this later by accident;
reopening it requires an explicit product decision.
