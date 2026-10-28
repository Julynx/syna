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

## Explicitly out of scope by design

### Cross-session persistent memory

Deliberately excluded as a privacy and safety feature. Memory that
survives a session introduces unpredictability and non-determinism into
what the agent carries into future work, and this is a cost the project
refuses on purpose. Within a single session, the `think` tool and the
notes/plan convention (`/workspace/notes/progress.md`) already cover the
scope persistent memory would serve. Do not "fix" this later by accident;
reopening it requires an explicit product decision.
