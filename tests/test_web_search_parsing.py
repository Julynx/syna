"""Unit tests for the web search result post-processing helpers."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna import web_search
from syna.web_search import _format_results, _is_challenge, _unwrap_bing, _unwrap_ddg


def test_unwrap_ddg_resolves_redirect_wrapper():
    href = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc"
    assert _unwrap_ddg(href) == "https://example.com/page"


def test_unwrap_ddg_keeps_plain_links():
    assert _unwrap_ddg("https://example.com") == "https://example.com"


def test_unwrap_bing_decodes_base64_redirect():
    import base64

    target = "https://example.com/deep/page?q=1"
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    href = f"https://www.bing.com/ck/a?!&u=a1{encoded}&ntb=1"
    assert _unwrap_bing(href) == target


def test_unwrap_bing_keeps_plain_links():
    assert _unwrap_bing("https://example.com") == "https://example.com"


def test_unwrap_bing_survives_malformed_payload():
    href = "https://www.bing.com/ck/a?!&u=a1%%%%&ntb=1"
    assert _unwrap_bing(href) == href


def test_format_results_renders_blocks():
    results = [
        {"title": "Title", "href": " https://example.com ", "body": " Body "},
        {"title": "T2", "href": "https://example.com/2", "body": "B2"},
    ]
    assert _format_results(results) == (
        "Title\nhttps://example.com\nBody\n\nT2\nhttps://example.com/2\nB2\n"
    )


def test_is_challenge_detects_widgets_and_markers():
    assert _is_challenge("widget")
    assert _is_challenge("verification required to continue")
    assert _is_challenge("our systems have detected unusual traffic")
    assert not _is_challenge("no results found for this query")
    assert not _is_challenge("")


def test_engines_in_cooldown_are_skipped():
    recorded = []
    engines = web_search._ENGINES
    original_engine_search = web_search._search_engine
    original_page_challenge = web_search._page_shows_challenge
    original_blocked = web_search._engine_blocked_until
    web_search._engine_blocked_until = {engines[0][0]: time.monotonic() + 999}
    web_search._search_engine = lambda query, engine: (
        recorded.append(engine[0])
        or [{"title": "t", "href": "https://example.com", "body": "b"}]
    )
    web_search._page_shows_challenge = lambda: False
    try:
        results = web_search._browser_search("query")
    finally:
        web_search._search_engine = original_engine_search
        web_search._page_shows_challenge = original_page_challenge
        web_search._engine_blocked_until = original_blocked
    assert results
    assert recorded == [engines[1][0]], (
        f"expected the second engine to serve, got {recorded}"
    )


def test_challenged_engine_gets_a_cooldown():
    calls = []
    engines = web_search._ENGINES
    test_blocked = {}
    original_engine_search = web_search._search_engine
    original_page_challenge = web_search._page_shows_challenge
    original_blocked = web_search._engine_blocked_until

    def _fake_engine_search(query, engine):
        calls.append(engine[0])
        return []

    class _ShimTime:
        """Skip the inter-engine pauses; delegate everything else."""

        def sleep(self, seconds):
            pass

        def __getattr__(self, name):
            return getattr(time, name)

    web_search._search_engine = _fake_engine_search
    web_search._page_shows_challenge = lambda: True
    web_search.time = _ShimTime()
    web_search._engine_blocked_until = test_blocked
    try:
        results = web_search._browser_search("query")
    finally:
        web_search._search_engine = original_engine_search
        web_search._page_shows_challenge = original_page_challenge
        web_search.time = time
        web_search._engine_blocked_until = original_blocked
    assert results == []
    assert calls == [engine[0] for engine in engines]
    for engine in engines:
        assert test_blocked[engine[0]] > time.monotonic(), engine[0]


def main():
    """Run every parsing test and report a summary."""
    run_suite(
        [
            test_unwrap_ddg_resolves_redirect_wrapper,
            test_unwrap_ddg_keeps_plain_links,
            test_unwrap_bing_decodes_base64_redirect,
            test_unwrap_bing_keeps_plain_links,
            test_unwrap_bing_survives_malformed_payload,
            test_format_results_renders_blocks,
            test_is_challenge_detects_widgets_and_markers,
            test_engines_in_cooldown_are_skipped,
            test_challenged_engine_gets_a_cooldown,
        ]
    )


if __name__ == "__main__":
    main()
