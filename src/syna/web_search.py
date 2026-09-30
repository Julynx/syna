from ddgs import DDGS

from .web_to_markdown import Config, fetch_markdown


def search(query):

    if query.startswith("https://"):
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
