"""Check whether Mojeek's ALTCHA proof-of-work auto-solves in Camoufox."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.capabilities.browser import Browser

COUNT_JS = '() => document.querySelectorAll("ul.results-standard li, li.result").length'
STATE_JS = (
    '() => { const w = document.querySelector(".altcha");'
    ' return w ? w.getAttribute("data-state") : "gone"; }'
)
DEADLINE_S = 20


def main():
    """Poll Mojeek for up to 20s, printing challenge state and result count."""
    browser = Browser(headless=True)
    try:
        browser.goto("https://www.mojeek.com/search?q=debian+13+release+notes")
        start = time.monotonic()
        while time.monotonic() - start < DEADLINE_S:
            count = browser.run(browser.page.evaluate(COUNT_JS))
            state = browser.run(browser.page.evaluate(STATE_JS))
            elapsed = time.monotonic() - start
            print(f"{elapsed:5.1f}s state={state:12} results={count}")
            if count:
                break
            time.sleep(1.5)
    finally:
        browser.close()


if __name__ == "__main__":
    main()
