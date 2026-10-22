"""Verify every engine in the web_search chain: parse results from each."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna import web_search

QUERY = "debian 13 release notes"


def main():
    """Run one search per engine and print outcome, count, and first hit."""
    for engine in web_search._ENGINES:
        start = time.monotonic()
        try:
            results = web_search._search_engine(QUERY, engine)
            first = (
                f"{results[0]['title'][:40]} -> {results[0]['href'][:50]}"
                if results
                else "-"
            )
            print(
                f"{engine[0]:10} {len(results):2} results  "
                f"{time.monotonic() - start:4.1f}s  {first}"
            )
        except Exception as exc:
            print(
                f"{engine[0]:10} FAIL  {type(exc).__name__}: {str(exc)[:90]}  "
                f"{time.monotonic() - start:4.1f}s"
            )
        time.sleep(2)


if __name__ == "__main__":
    main()
