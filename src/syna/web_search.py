"""Web search and URL-to-markdown fetching for the agent tools."""

import re
import time

from ddgs import DDGS

from .web_to_markdown import Config, fetch_markdown

_URL_PATTERN = re.compile(r"^https?://\S+$", re.IGNORECASE)


def search(query, delay_s=2):
    """Return page markdown for URLs, or DuckDuckGo results for queries.

    Queries starting with http:// or https:// are fetched, rendered in a
    headless browser and converted to markdown; everything else goes to
    DuckDuckGo text search.

    Sleeps for 'delay_s' before every search to avoid getting rate-limited.
    """
    time.sleep(delay_s)

    query = query.strip()
    if _URL_PATTERN.match(query):
        return fetch_markdown(query, Config(headless=True))

    results = DDGS().text(query, max_results=10)
    if not results:
        return "Error performing web search"

    output_lines = []
    for result in results:
        output_lines.append(f"{result['title'].strip()}")
        output_lines.append(f"{result['href'].strip()}")
        output_lines.append(f"{result['body'].strip()}")
        output_lines.append("")

    return "\n".join(output_lines)
