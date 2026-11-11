# Improvement Backlog

Researched improvements that have potential but are not implemented. Each
entry records the evidence, the fit to Syna, and what blocks it. Do not
implement an entry without re-checking the research: the field moves fast,
and the numbers below have expiry dates.

## Configurable pod permissions (network, privileges): study if worth it

Per-session policy for what the pod may do: network egress, reachability
of the host and engine bridge, and privilege level inside the container.
The word "permissions" is deliberate: this is about what the sandbox
may touch, not what the model may say.

The problem, precisely: the pod currently runs as root with unrestricted
network access. The only ceilings are resource-shaped (`pod_memory_mb`,
`pod_pids_limit`), which bound runaway processes but not intent. The
host-side browser, by contrast, already runs behind a full network
policy engine (`net_guard`: private ranges blocked, hostname resolution
checked at decision time, allowlist config, tested matrix in
`test_net_guard.py`). The asymmetry is real and documented; the server
section of the technical description lists pod egress as an accepted
residual risk and calls it "a separate topic". This entry is that
separate topic.

Why it is not a obvious yes: the pod's most common legitimate work:
`pip install`, `apt-get`, `git clone`, `curl`, needs network, and much
of it needs root. A permissions model that breaks package installation
breaks the agent. The study has to answer whether the safety gain
outweighs the workflow cost, hence "study if worth it" rather than
roadmap.

The design space to evaluate:

- **Egress modes**, coarse to fine: off / public-only (reuse
  `net_guard`'s blocked-range matrix: it already exists, is tested, and
  is pod-agnostic by design) / per-destination allowlist. The reuse path
  is the cheap one: the policy engine is written against destinations,
  not against the browser.
- **Enforcement point.** Engine-level (a custom podman network whose
  default route is blocked or NATed through a filter) is tamper-proof
  but per-session and heavier to set up; in-pod nftables is cheap but
  the model is root and can undo it; the docs already draw the
  integrity-vs-boundary line for read-only artifacts, and the same
  reasoning applies: in-pod rules are an integrity barrier, not a
  security boundary. DNS-level filtering (block resolution of private
  names) is a middle ground that mirrors `net_guard`'s hostname check.
- **Privilege shaping.** Unprivileged user, `CapDrop`, read-only
  rootfs variants. Evaluate what actually breaks: `apt-get install`
  needs root; most build work does not. A `root_required` escape hatch
  (per-call, user-approved) may be worth more than a global downgrade.
- **Approval UX.** The `ask_user` interaction bridge is the natural
  consent point: "Syna wants to download from pypi.org. Allow?" gives
  the user a real decision instead of a config file they set once and
  forget. Study whether per-destination prompts are annoying enough to
  defeat the feature.
- **Denial ergonomics.** Whatever is denied must come back to the model
  as descriptive result text (the `ERROR:` convention), naming what was
  blocked and how to ask for it; otherwise the model retries and
  burns steps.

Prerequisites: decide per-session vs per-category granularity before
any code; measure whether engine-level network setup per session is
fast enough to keep session start snappy; check what Podman rootless
machines already isolate (the podman VM already blocks some host
reachability; verify before building what it gives for free).

## `web_search` filters (recency, domain)

Optional parameters on `search`: a recency window (e.g. `recency="month"`)
and a domain restriction (`site=`), mapped to the engine endpoints that
support them (DuckDuckGo html accepts a `df` parameter; Bing has `qft`
filters), ignored by engines that do not.

Evidence: a live session (2026-11-05) ranked date filtering her #5
request ("for current-facts tasks, filtering by date is almost always
the first impulse"), but the search experiments in that same session
never ran (host network outage), so the demand is stated, not measured.

Why backlog: the engine fleet is server-rendered HTML endpoints whose
parameters shift without notice, and unsupported-engine fallback must be
honest (silently ignoring a recency filter returns stale results dressed
as fresh). Instrument real sessions first: if queries about current
facts are rare, the browser `visit` path plus a query word ("2026") is
an adequate substitute.
