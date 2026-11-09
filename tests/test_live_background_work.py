"""Live end-to-end check of the background-work fixes (real pod, real model).

Run explicitly: uv run python tests/test_live_background_work.py

Covers, in order:
1. A 5s runtime cap reports "after 5s" from the pod-clock stamps (was 4s).
2. wait_for that sleeps to a finish says "It finished during this wait."
3. wait_for on work that finished between waits says "finished Ns after
   your previous wait ended."
4. A model-killed process reports exit 143 with a stamp-based runtime.
5. A real subagent finishes and wait_for collects it (exactly-once notice).
6. A model-killed subagent: wait_for blocks until the stop lands, returns
   "stopped by the model after Ns", and the completion notice reaches the
   caller saying no response was produced.
"""

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from syna.config import get_env


def _live_key_present() -> bool:
    """Whether a real model key is configured.

    Checks the environment first, then the loaded .env (the CI workflow
    writes a dummy key there). Without a real key the subagent checks
    degrade into error-text "responses" and the run is meaningless, so
    the test exits skipped instead.
    """
    key = os.environ.get("OPENROUTER_API_KEY") or get_env().get(
        "OPENROUTER_API_KEY"
    )
    return bool(key) and key.startswith("sk-or")


if not _live_key_present():
    print("SKIP test_live_background_work (needs a real OPENROUTER_API_KEY)")
    sys.exit(0)

from syna.capabilities import tools
from syna.capabilities.processes import registry_for
from syna.capabilities.subagents import SubagentRecord
from syna.state import (
    create_container,
    destroy_container,
    register_container,
    unregister_container,
)

RESULTS = []


def report(label, ok, detail=""):
    RESULTS.append((label, ok))
    print(f"{'PASS' if ok else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""))


def poll_process(name, timeout_s=30):
    """Check until the process leaves running; return the last report."""
    deadline = time.time() + timeout_s
    text = ""
    while time.time() < deadline:
        text = tools.process("check", {"name": name})
        if "running" not in text.split("\n")[0]:
            return text
        time.sleep(1)
    return text


def wait_for_sink(seen, name, timeout_s=20):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for payload in seen:
            if payload.get("id") == name:
                return payload
        time.sleep(0.5)
    return None


def main():
    print("Creating container...")
    container = create_container()
    register_container(container)
    registry = registry_for(container)
    seen = []
    registry.completion_sink = lambda payload: seen.append(payload)
    try:
        # 1. Cap runtime honesty (the "after 4s" bug).
        tools.process(
            "spawn", {"command": "sleep 20", "name": "cap5", "max_runtime_s": 5}
        )
        stamp = registry.processes["cap5"].started_at
        report("spawn reads the pod started_at stamp", stamp is not None, repr(stamp))
        text = poll_process("cap5")
        match = re.search(r"exit code 124\) after (\d+)s", text)
        report(
            "5s cap reports ~5s from pod stamps",
            bool(match) and 4 <= int(match.group(1)) <= 7,
            text.split("\n")[0],
        )

        # 2. Genuine wake sentence.
        tools.process("spawn", {"command": "sleep 2", "name": "quick2"})
        text = tools.wait_for(name="quick2", max_wait_s=30)
        report(
            "wait_for wake says 'finished during this wait'",
            "It finished during this wait." in text,
            text.split("\n")[-1],
        )

        # 3. Between-waits gap sentence.
        tools.process("spawn", {"command": "sleep 8", "name": "gap8"})
        first = tools.wait_for(name="gap8", max_wait_s=3)
        report(
            "short wait times out honestly",
            "still running after waiting 3s" in first,
            first.split("\n")[0],
        )
        time.sleep(7)
        second = tools.wait_for(name="gap8", max_wait_s=30)
        match = re.search(r"finished (\d+)s after your previous wait ended", second)
        report(
            "gap sentence reports the between-waits finish",
            bool(match) and 2 <= int(match.group(1)) <= 8,
            second.split("\n")[-1],
        )

        # 4. Model-killed process: the kill blocks and reports the final code.
        tools.process("spawn", {"command": "sleep 60", "name": "killme"})
        time.sleep(2)
        kill_start = time.monotonic()
        killed = tools.process("kill", {"name": "killme"})
        block = time.monotonic() - kill_start
        report(
            "process kill reports the final code itself",
            "143" in killed and "still terminating" not in killed,
            killed.split("\n")[0],
        )
        report(
            "process kill blocked for the exit",
            block <= 10,
            f"{block:.1f}s",
        )

        # 4b. A TERM-resistant process dies to the SIGKILL follow-up.
        tools.process(
            "spawn",
            {"command": "trap '' TERM; sleep 60", "name": "stubborn"},
        )
        time.sleep(2)
        stubborn_start = time.monotonic()
        stubborn = tools.process("kill", {"name": "stubborn"})
        stubborn_block = time.monotonic() - stubborn_start
        report(
            "TERM-resistant process dies to SIGKILL with the final code",
            "137" in stubborn and "still terminating" not in stubborn,
            stubborn.split("\n")[0],
        )
        report(
            "the stubborn kill blocked through the SIGKILL grace",
            stubborn_block >= 2,
            f"{stubborn_block:.1f}s",
        )

        # 5. Real subagent, natural completion.
        result = tools.subagent(
            "spawn",
            {"goal": "Reply with exactly the single word: ready", "name": "live-quick"},
        )
        text = tools.wait_for(name="live-quick", max_wait_s=180)
        report(
            "subagent finishes and wait_for collects it",
            "finished with a response" in text,
            text.split("\n")[0],
        )
        payload = wait_for_sink(seen, "live-quick")
        report(
            "natural completion notice reaches the caller",
            payload is not None
            and payload.get("summary") == "finished with a response",
            repr(payload.get("summary")) if payload else "no payload",
        )

        # 6. Subagent kill: wait_for blocks, notice says no response.
        tools.subagent(
            "spawn",
            {
                "goal": (
                    "Call the think tool 15 times. Each thought must contain the"
                    " next number of a count starting at 1 (so the first thought"
                    " contains 1, the second 2, and so on). After the 15th, reply"
                    " with the single word: done. Use no other tool."
                ),
                "name": "live-kill",
            },
        )
        time.sleep(6)
        record = registry.subagents.get("live-kill")
        if record is not None and record.state != "running":
            report(
                "kill test skipped: subagent finished before the kill",
                True,
                record.state,
            )
        else:
            killed = tools.subagent("kill", {"name": "live-kill"})
            report(
                "kill response requests the stop",
                "stop requested" in killed,
                killed.split("\n")[0],
            )
            waited = tools.wait_for(name="live-kill", max_wait_s=90)
            report(
                "wait_for blocks until the stop lands",
                "stopped by the model after" in waited,
                waited.split("\n")[0],
            )
            payload = wait_for_sink(seen, "live-kill")
            notice = payload.get("model_notice", "") if payload else ""
            # Which artifact wording appears depends on whether the
            # subagent emitted assistant content before stopping (the
            # last-assistant fallback gives it a response.md); both are
            # honest. The offline test pins each wording deterministically.
            ok = (
                payload is not None
                and payload.get("summary") == "stopped by the model"
                and (
                    "produced no final response" in notice
                    or ("Its response:" in notice and "messages.json" in notice)
                )
            )
            report(
                "kill-landing notice reaches the caller with honest artifacts",
                ok,
                repr(payload.get("summary")) if payload else "no payload",
            )
    finally:
        destroy_container(container)
        unregister_container()

    failed = [label for label, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
