"""Live browser health checks: read-workflow reliability and stealth.

Phase 1 reproduces the exact workflow that failed in the investigate/ session
(goto a heavy Fandom page, then read the full page text) and soaks the page
with periodic reads to catch renderer crashes or popup hijacks.

Phase 2 visits common bot-detection pages and dumps their verdicts, so a
human can confirm the "functional and undetectable" standard.

Run with:
    uv run python tests/test_browser_health.py [--stealth]
"""

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from syna.browser import BrowserSession

SOAK_SECONDS = 90
SOAK_INTERVAL_S = 8
LEAGUEPEDIA_URL = "https://lol.fandom.com/wiki/World_Championship"
BLANK_READ_MARKER = "no text returned"


async def phase_read_workflow() -> list[str]:
    """Goto the heavy page and soak it with reads, reporting every anomaly."""
    problems = []
    async with BrowserSession() as session:
        print("[goto]", LEAGUEPEDIA_URL)
        view = await session.call("goto", {"url": LEAGUEPEDIA_URL})
        print(view.splitlines()[0] if view else "(empty view)")

        print("[read] full page text")
        text = await session.call("read", {"max_chars": 6000})
        if not text.strip():
            problems.append("read returned empty right after goto")
        elif BLANK_READ_MARKER in text:
            problems.append("read returned the blank-document diagnostic")
        else:
            print(
                f"[read] ok, {len(text)} chars, first line: {text.splitlines()[0][:90]}"
            )

        print("[screenshot] capture")
        data = await session.screenshot_bytes()
        if not data.startswith(b"\x89PNG"):
            problems.append("screenshot_bytes did not return PNG data")
        else:
            print(f"[screenshot] ok, {len(data) // 1024} KB PNG")

        started = time.monotonic()
        cycle = 0
        while time.monotonic() - started < SOAK_SECONDS:
            time.sleep(SOAK_INTERVAL_S)
            cycle += 1
            page = session.page
            if page.is_closed():
                problems.append(f"cycle {cycle}: current page was closed")
                break
            text = await session.call("read", {"max_chars": 2000})
            if not text.strip() or BLANK_READ_MARKER in text:
                problems.append(f"cycle {cycle}: read came back empty/blank")
            title = await session._live_page().title()
            url = session._live_page().url
            print(
                f"[soak {cycle}] url={url[:80]} title={title[:60]!r} read={len(text)} chars"
            )
            if url != LEAGUEPEDIA_URL:
                problems.append(f"cycle {cycle}: page navigated away to {url}")
    return problems


async def phase_stealth() -> None:
    """Visit bot-detection pages and dump their verdicts for human review."""
    async with BrowserSession() as session:
        for url in (
            "https://bot.sannysoft.com/",
            "https://abrahamjuliot.github.io/creepjs/",
        ):
            print(f"\n=== {url} ===")
            view = await session.call("goto", {"url": url})
            await asyncio.sleep(5)
            text = await session.call("read", {"max_chars": 4000})
            print(text if text.strip() else view[:2000])


async def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"Browser health check — {stamp}\n")

    problems = await phase_read_workflow()

    print("\n" + "=" * 60)
    if problems:
        print("READ WORKFLOW: PROBLEMS DETECTED")
        for problem in problems:
            print(" -", problem)
    else:
        print("READ WORKFLOW: OK (no crashes, hijacks, or empty reads)")
    print("=" * 60)

    if "--stealth" in sys.argv:
        await phase_stealth()


if __name__ == "__main__":
    asyncio.run(main())
