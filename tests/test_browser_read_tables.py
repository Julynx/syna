"""Live tests for the table-aware text extraction in browser.tool_read.

Launches Camoufox once and evaluates TABLE_AWARE_TEXT_JS against static
HTML (set_content, no navigation, no network): tables must render one line
per row with cells joined by " | ", non-table text must survive, and the
extraction must leave the DOM untouched.

Run with:
    uv run python tests/test_browser_read_tables.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from camoufox.async_api import AsyncCamoufox

from syna.capabilities.browser import SNAPSHOT_JS, TABLE_AWARE_TEXT_JS

CENSUS_HTML = """
<html><body>
  <h1>World butterfly census</h1>
  <p>Numbers per biogeographic region.</p>
  <table>
    <thead>
      <tr><th>Group</th><th>World</th><th>Holarctic</th></tr>
    </thead>
    <tbody>
      <tr><td>HESPERIIDAE</td><td>4127</td><td>497</td></tr>
      <tr><td>Hesperiinae</td><td>2117</td><td>275</td></tr>
      <tr><td>Papilioninae</td><td>547</td><td>80</td></tr>
    </tbody>
  </table>
  <table>
    <tr><td>empty cells </td><td></td><td> stay put</td></tr>
    <tr><td>   </td><td>row of blanks is dropped</td></tr>
  </table>
  <p>Footer prose after the tables.</p>
  <div style="display: none"><table><tr><td>hidden</td></tr></table></div>
</body></html>
"""


async def extract(html: str) -> str:
    cm = AsyncCamoufox(headless=True)
    browser = await cm.__aenter__()
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(html)
        return await page.evaluate(TABLE_AWARE_TEXT_JS, "body")
    finally:
        await cm.__aexit__(None, None, None)


def test_tables_render_one_row_per_line():
    text = asyncio.run(extract(CENSUS_HTML))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    assert "Group | World | Holarctic" in lines
    assert "HESPERIIDAE | 4127 | 497" in lines
    assert "Hesperiinae | 2117 | 275" in lines
    assert "Papilioninae | 547 | 80" in lines
    print("PASS tables render one row per line with | separators")


def test_prose_and_edge_cases_survive():
    text = asyncio.run(extract(CENSUS_HTML))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    assert "World butterfly census" in lines
    assert "Numbers per biogeographic region." in lines
    assert "Footer prose after the tables." in lines
    assert "empty cells |  | stay put" in lines
    assert "| row of blanks is dropped" in lines
    assert all("hidden" not in line for line in lines)
    print("PASS prose, empty cells, and hidden subtrees behave")


def test_extraction_does_not_mutate_the_dom():
    async def run():
        cm = AsyncCamoufox(headless=True)
        browser = await cm.__aenter__()
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content(CENSUS_HTML)
            before = await page.content()
            await page.evaluate(TABLE_AWARE_TEXT_JS, "body")
            return before == await page.content()
        finally:
            await cm.__aexit__(None, None, None)

    assert asyncio.run(run()), "extraction must not mutate the DOM"
    print("PASS extraction leaves the DOM untouched")


def test_view_renders_full_table_rows_despite_source_whitespace():
    """Regression: the line budget must count visible text, not raw source.

    The census page wraps every cell in <font>/<p> with newline-indent
    between tags; raw-length budgeting clipped rows after ~3 cells.
    """

    async def run():
        cm = AsyncCamoufox(headless=True)
        browser = await cm.__aenter__()
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content(CENSUS_HTML)
            return (
                await page.evaluate(
                    SNAPSHOT_JS, {"viewportOnly": False, "maxLine": 240}
                )
            )["lines"]
        finally:
            await cm.__aexit__(None, None, None)

    lines = [line.strip() for line in asyncio.run(run())]
    table_lines = [line for line in lines if "HESPERIIDAE" in line]
    assert table_lines == ["HESPERIIDAE | 4127 | 497"], table_lines
    header = next(line for line in lines if "Holarctic" in line)
    assert header == "Group | World | Holarctic", header
    print("PASS view renders full table rows regardless of source whitespace")


def main():
    """Run every table-read test and report a summary."""
    test_tables_render_one_row_per_line()
    test_prose_and_edge_cases_survive()
    test_extraction_does_not_mutate_the_dom()
    test_view_renders_full_table_rows_despite_source_whitespace()
    print("ALL BROWSER TABLE-READ TESTS PASSED")


if __name__ == "__main__":
    main()
