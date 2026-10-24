"""Live diagnostic for the stealth-browser web search chain (manual run).

Runs the real `web_search.search()` path (pacing included) over a fixed
query set several times, records outcome and latency per attempt, and
prints a summary. Use it after touching web_search.py or the engines,
or whenever search reliability is in question.

Run with:
    uv run python tests/test_live_search_diagnose.py [rounds]
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.capabilities import web_search

QUERIES = [
    "debian 13 release notes",
    "python 3.14 new features",
    "how does a heat pump work",
    "rust vs c++ performance benchmarks",
    "who wrote the three body problem",
    "fastapi websocket authentication",
    "duckduckgo html endpoint 403",
    "playwright vs puppeteer 2026",
    "italian verb essere conjugation",
    "cpu transistor size timeline",
]

REPORT_PATH = Path(tempfile.gettempdir()) / "syna_web_search_diagnosis.jsonl"


def classify(results, error):
    """Label one search attempt."""
    if error is not None:
        return f"exception:{type(error).__name__}"
    if isinstance(results, str) and results.startswith("Error"):
        return "no-engine-returned-results"
    if not results.strip():
        return "blank"
    return "ok"


def run_search(round_index, query):
    """Run one search and return a report entry."""
    start = time.monotonic()
    error = None
    results = None
    try:
        results = web_search.search(query)
    except Exception as exc:
        error = exc
    latency = time.monotonic() - start
    entry = {
        "round": round_index,
        "query": query,
        "outcome": classify(results, error),
        "latency_s": round(latency, 2),
        "exception": repr(error) if error else None,
        "result_chars": len(results) if isinstance(results, str) else 0,
    }
    print(f"[r{round_index}] {entry['outcome']:28} {entry['latency_s']:>6}s  {query}")
    if error:
        print(f"      exception: {entry['exception']}")
    return entry


def main():
    """Run every query several times and summarize the failure profile."""
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    entries = []
    with REPORT_PATH.open("w", encoding="utf-8") as report:
        for round_index in range(rounds):
            for query in QUERIES:
                entry = run_search(round_index, query)
                entries.append(entry)
                report.write(json.dumps(entry, ensure_ascii=False) + "\n")
                report.flush()

    total = len(entries)
    failed = [e for e in entries if e["outcome"] != "ok"]
    latencies = [e["latency_s"] for e in entries]
    print(
        f"\n{total - len(failed)}/{total} searches succeeded "
        f"({len(failed)} failures = {len(failed) / total:.0%})"
    )
    print(
        f"latency avg {sum(latencies) / len(latencies):.1f}s max {max(latencies):.1f}s"
    )
    for outcome in sorted({e["outcome"] for e in failed}):
        queries = [e["query"] for e in failed if e["outcome"] == outcome]
        print(f"\n{outcome} ({len(queries)}x):")
        for query in queries:
            print(f"  - {query}")
    print(f"\nFull report: {REPORT_PATH}")


if __name__ == "__main__":
    main()
