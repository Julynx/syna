"""Live test for the stealth-browser search chain (manual run).

Phase 1 verifies every extractor against representative markup fixtures
loaded into the shared browser (deterministic, independent of the live
engines' mood).

Phase 2 covers the chain against the live engines: ddg-html, ddg-lite,
and bing must parse results; mojeek is best-effort only — it may serve an
ALTCHA proof-of-work challenge page, which the chain treats as "no
results" and moves past (auto-clicking the challenge would be bot
behavior, and mojeek only matters if the earlier engines fail).

Phase 3 verifies the fallback: a broken engine at the front of the chain
must not fail the search (a later engine answers), and a chain where
every engine is broken degrades gracefully to the 'Error performing web
search' result string instead of raising.

Run with:
    uv run python tests/test_live_web_search.py
"""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna import web_search

QUERY = "debian 13 release notes"
BROKEN_ENGINE = (
    "broken",
    "https://broken.invalid/search?q={query}",
    "https://broken.invalid/",
    web_search._EXTRACT_DDG_HTML_JS,
    web_search._unwrap_ddg,
)

MOJEEK_FIXTURE = """
<html><body><ul class="results-standard">
<li><h2><a class="title" href="https://example.com/debian">Debian 13</a></h2>
<p class="s">Release notes for Debian 13.</p></li>
<li><h2><a class="title" href="https://example.org/notes">More notes</a></h2>
<p class="s">Another snippet.</p></li>
</ul></body></html>
"""

DDG_HTML_FIXTURE = """
<html><body>
<div class="result result--ad"><a class="result__a"
 href="https://ads.example.com">Ad</a></div>
<div class="result"><a class="result__a"
 href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdebian&rut=x">
Debian 13</a><a class="result__snippet">Stable release notes.</a></div>
<div class="result"><h2><a href="https://example.org/notes">Wiki page</a></h2>
<a class="result__snippet">Community notes.</a></div>
</body></html>
"""

DDG_LITE_FIXTURE = """
<html><body><table>
<tr><td><a class="result-link" href="https://example.com/debian">Debian 13</a></td></tr>
<tr><td class="result-snippet">Stable release notes.</td></tr>
<tr><td><a class="result-link" href="https://example.org/notes">More notes</a></td></tr>
<tr><td class="result-snippet">Another snippet.</td></tr>
</table></body></html>
"""

BING_FIXTURE = """
<html><body><ol>
<li class="b_algo">
<h2><a href="https://www.bing.com/ck/a?!&u=a1{}&ntb=1">Debian 13</a></h2>
<div class="b_caption"><p>Stable release notes.</p></div>
</li>
<li class="b_algo">
<h2><a href="https://example.org/notes">More notes</a></h2>
<div class="b_caption"><p>Another snippet.</p></div>
</li>
</ol></body></html>
"""


def _bing_fixture_href(target: str) -> str:
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    return BING_FIXTURE.format(encoded)


def test_extractors_parse_their_fixtures():
    browser = web_search._get_browser()
    page = browser.page
    cases = [
        ("ddg-html", web_search._EXTRACT_DDG_HTML_JS, DDG_HTML_FIXTURE),
        ("ddg-lite", web_search._EXTRACT_DDG_LITE_JS, DDG_LITE_FIXTURE),
        (
            "bing",
            web_search._EXTRACT_BING_JS,
            _bing_fixture_href("https://example.com/debian"),
        ),
        ("mojeek", web_search._EXTRACT_MOJEEK_JS, MOJEEK_FIXTURE),
    ]
    for name, extract_js, fixture in cases:
        browser.run(page.set_content(fixture, wait_until="domcontentloaded"))
        raw = browser.run(page.evaluate(extract_js))
        results = [dict(item, href=engine_unwrap(name, item["href"])) for item in raw]
        assert len(results) == 2, f"'{name}': expected 2 results, got {results}"
        assert results[0]["title"] == "Debian 13", name
        assert results[0]["href"] == "https://example.com/debian", name
        assert results[0]["body"], name
        assert results[1]["href"] == "https://example.org/notes", name


def engine_unwrap(name, href):
    """Apply the engine's unwrap by name (mirrors the _ENGINES wiring)."""
    for engine_name, _url, _referer, _js, unwrap in web_search._ENGINES:
        if engine_name == name:
            return unwrap(href)
    return href


def test_live_engines_return_results():
    required = ["ddg-html", "ddg-lite", "bing"]
    for engine in web_search._ENGINES:
        results = web_search._search_engine(QUERY, engine)
        if engine[0] == "mojeek":
            if not results:
                print(
                    "NOTE mojeek returned no results "
                    "(likely ALTCHA challenge) — best-effort, skipping"
                )
                continue
        else:
            assert engine[0] in required
            assert results, f"engine '{engine[0]}' returned no results"
            assert len(results) >= 3, (
                f"engine '{engine[0]}' returned only {len(results)} results"
            )
        for result in results:
            assert result["title"], f"engine '{engine[0]}': empty title"
            assert result["href"].startswith("http"), (
                f"engine '{engine[0]}': non-http href {result['href']!r}"
            )


def test_broken_first_engine_falls_through():
    original_engines = web_search._ENGINES
    web_search._ENGINES = (BROKEN_ENGINE,) + original_engines
    try:
        results = web_search._browser_search(QUERY)
    finally:
        web_search._ENGINES = original_engines
    assert results, "search failed despite healthy engines behind the broken one"
    assert any(
        "debian" in r["href"].lower() or "debian" in r["title"].lower() for r in results
    ), "fallback results do not match the query"


def test_all_engines_broken_degrades_gracefully():
    original_engines = web_search._ENGINES
    web_search._ENGINES = (BROKEN_ENGINE,)
    try:
        results = web_search.search(QUERY, delay_s=0.1)
    finally:
        web_search._ENGINES = original_engines
    assert results == "Error performing web search"


def main():
    """Run the live chain tests and report a summary."""
    run_suite(
        [
            test_extractors_parse_their_fixtures,
            test_live_engines_return_results,
            test_broken_first_engine_falls_through,
            test_all_engines_broken_degrades_gracefully,
        ]
    )


if __name__ == "__main__":
    main()
