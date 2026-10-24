"""Web search for the agent tools.

Queries run through the stealth browser (Camoufox) against server-rendered
search endpoints. Engines are tried in order until one returns results: a
real browser fingerprint passes the anomaly checks that block plain HTTP
clients, and falling back across independent providers through real
navigations is exactly what a person retrying on another search engine
looks like — unlike a bot-shaped HTTP library, whose failures are the
requests most likely to earn the IP a place on a bot list.

Every wait is randomized (never fixed) so request timing carries no
metronomic signature. Engines that serve a captcha/challenge page are
paused for a randomized cooldown instead of being re-requested on the
next search. The browser is created lazily and kept warm for the process
lifetime; searches share a process-wide pacing lock.
"""

import atexit
import random
import re
import threading
import time
from base64 import urlsafe_b64decode
from urllib.parse import parse_qs, quote_plus, urlparse

from .browser import Browser
from ..config import get_logger

_URL_PATTERN = re.compile(r"^https?://\S+$", re.IGNORECASE)

_MAX_RESULTS = 10

_ENGINE_COOLDOWN_S = (15 * 60, 30 * 60)

_CHALLENGE_PROBE_JS = """
() => {
  const widget = document.querySelector(
    '[class*="altcha" i], iframe[src*="challenges"], iframe[src*="hcaptcha"],'
    + ' iframe[src*="recaptcha"], .g-recaptcha, #challenge-form, .captcha-box'
  );
  if (widget) return "widget";
  return document.body ? document.body.innerText.slice(0, 600).toLowerCase() : "";
}
"""

_CHALLENGE_TEXT_MARKERS = (
    "verification required",
    "verify you are human",
    "confirm you are human",
    "are you a robot",
    "unusual traffic",
    "captcha",
    "just a moment",
    "checking your browser",
    "attention required",
)

_EXTRACT_DDG_HTML_JS = """
() => {
  const out = [];
  for (const item of document.querySelectorAll('.result')) {
    if (item.classList.contains('result--ad')) continue;
    const a = item.querySelector('a.result__a') || item.querySelector('h2 a');
    if (!a) continue;
    const snip = item.querySelector('.result__snippet');
    out.push({
      title: a.innerText.trim(),
      href: a.href,
      body: snip ? snip.innerText.trim() : ''
    });
  }
  return out;
}
"""

_EXTRACT_DDG_LITE_JS = """
() => {
  const out = [];
  for (const a of document.querySelectorAll('a.result-link')) {
    const row = a.closest('tr');
    const snippetRow = row && row.nextElementSibling;
    const snip = snippetRow && snippetRow.querySelector('.result-snippet');
    out.push({
      title: a.innerText.trim(),
      href: a.href,
      body: snip ? snip.innerText.trim() : ''
    });
  }
  return out;
}
"""

_EXTRACT_BING_JS = """
() => {
  const out = [];
  for (const li of document.querySelectorAll('li.b_algo')) {
    const a = li.querySelector('h2 a');
    if (!a) continue;
    const p = li.querySelector('.b_caption p') || li.querySelector('p');
    out.push({
      title: a.innerText.trim(),
      href: a.href,
      body: p ? p.innerText.trim() : ''
    });
  }
  return out;
}
"""

_EXTRACT_MOJEEK_JS = """
() => {
  const out = [];
  for (const li of document.querySelectorAll('ul.results-standard li')) {
    const a = li.querySelector('a.title') || li.querySelector('h2 a');
    if (!a) continue;
    const p = li.querySelector('p.s');
    out.push({
      title: a.innerText.trim(),
      href: a.href,
      body: p ? p.innerText.trim() : ''
    });
  }
  return out;
}
"""


def _unwrap_ddg(href: str) -> str:
    """Resolve DuckDuckGo's /l/?uddg= redirect wrapper to the target URL."""
    if "duckduckgo.com/l/" not in href:
        return href
    wrapped = parse_qs(urlparse(href).query).get("uddg", [None])[0]
    return wrapped or href


def _unwrap_bing(href: str) -> str:
    """Decode Bing's /ck/a redirect (base64url in the u= parameter)."""
    if "bing.com/ck/a" not in href:
        return href
    wrapped = parse_qs(urlparse(href).query).get("u", [None])[0]
    if not wrapped or not wrapped.startswith("a1"):
        return href
    encoded = wrapped[2:] + "=" * (-len(wrapped[2:]) % 4)
    try:
        target = urlsafe_b64decode(encoded).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):
        return href
    return target if target.startswith(("http://", "https://")) else href


_ENGINES = (
    (
        "ddg-html",
        "https://html.duckduckgo.com/html/?q={query}",
        "https://duckduckgo.com/",
        _EXTRACT_DDG_HTML_JS,
        _unwrap_ddg,
    ),
    (
        "ddg-lite",
        "https://lite.duckduckgo.com/lite/?q={query}",
        "https://duckduckgo.com/",
        _EXTRACT_DDG_LITE_JS,
        _unwrap_ddg,
    ),
    (
        "bing",
        "https://www.bing.com/search?q={query}",
        "https://www.bing.com/",
        _EXTRACT_BING_JS,
        _unwrap_bing,
    ),
    (
        "mojeek",
        "https://www.mojeek.com/search?q={query}",
        "https://www.mojeek.com/",
        _EXTRACT_MOJEEK_JS,
        lambda href: href,
    ),
)

_search_lock = threading.Lock()

_browser: Browser | None = None

_engine_blocked_until: dict[str, float] = {}


def search(query, delay_s=2):
    """Return web search results for a query.

    Everything is searched through the stealth browser, trying DuckDuckGo
    (html, then lite), Bing, and Mojeek in order until one returns results.
    Up to 10 title/URL/snippet blocks come back. URLs are not queries: for
    those, the browser tool's `visit` command opens the page.

    Sleeps a randomized duration between 'delay_s' and 1.5x that before
    every search to avoid getting rate-limited; fixed waits would themselves
    be a fingerprint. The sleep and the request share a process-wide lock,
    so searches from concurrent sessions or detached calls run one after
    another and stay at least 'delay_s' apart instead of firing together.
    """
    with _search_lock:
        time.sleep(random.uniform(delay_s, delay_s * 1.5))
        return _search(query)


def _search(query):
    query = query.strip()
    if _URL_PATTERN.match(query):
        raise ValueError(
            "URLs are not search queries. To open a page, use the browser"
            " tool with command 'visit'."
        )

    results = _browser_search(query)
    if not results:
        return "Error performing web search"
    return _format_results(results)


def _browser_search(query):
    """Run the engine chain in the stealth browser; return result dicts.

    Engines that recently served a challenge page are skipped for a
    randomized cooldown: re-requesting a host that demands verification
    at search frequency is the repeat-offense pattern that hardens soft
    challenges into IP bans.
    """
    for index, engine in enumerate(_ENGINES):
        name = engine[0]
        blocked_until = _engine_blocked_until.get(name, 0.0)
        if blocked_until > time.monotonic():
            get_logger().info(
                "Search engine '%s' is cooling down after a challenge; skipping",
                name,
            )
            continue
        try:
            results = _search_engine(query, engine)
        except Exception:
            get_logger().exception(
                "Search engine '%s' failed for query %.100s", name, query
            )
            results = []
        if results:
            return results
        if _page_shows_challenge():
            cooldown = random.uniform(*_ENGINE_COOLDOWN_S)
            _engine_blocked_until[name] = time.monotonic() + cooldown
            get_logger().info(
                "Search engine '%s' served a challenge; pausing it for %.0fs",
                name,
                cooldown,
            )
        if index < len(_ENGINES) - 1:
            time.sleep(random.uniform(0.5, 1.5))
    return []


def _page_shows_challenge() -> bool:
    """Whether the current page is a captcha/challenge wall."""
    browser = _get_browser()
    probe = browser.run(browser.page.evaluate(_CHALLENGE_PROBE_JS))
    return _is_challenge(probe)


def _is_challenge(probe) -> bool:
    """Classify a challenge probe: a widget hit or a marker phrase."""
    return probe == "widget" or any(
        marker in probe for marker in _CHALLENGE_TEXT_MARKERS
    )


def _search_engine(query, engine):
    """Search one engine and return its parsed results."""
    _name, url_template, referer, extract_js, unwrap = engine
    browser = _get_browser()
    page = browser.page
    browser.run(
        page.goto(
            url_template.format(query=quote_plus(query)),
            wait_until="domcontentloaded",
            timeout=30000,
            referer=referer,
        )
    )
    time.sleep(random.uniform(0.7, 1.5))
    raw = browser.run(page.evaluate(extract_js))
    results = [dict(item, href=unwrap(item["href"])) for item in raw]
    return [r for r in results if r["href"]][:_MAX_RESULTS]


def _get_browser():
    """Create the shared search browser on first use."""
    global _browser
    if _browser is None:
        _browser = Browser(headless=True)
        atexit.register(_close_browser)
    return _browser


def _close_browser():
    """Shut the shared search browser down during interpreter exit."""
    global _browser
    browser, _browser = _browser, None
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass


def _format_results(results) -> str:
    """Render result dicts as the title/URL/snippet blocks the model sees."""
    output_lines = []
    for result in results:
        output_lines.append(result["title"].strip())
        output_lines.append(result["href"].strip())
        output_lines.append(result["body"].strip())
        output_lines.append("")
    return "\n".join(output_lines)
