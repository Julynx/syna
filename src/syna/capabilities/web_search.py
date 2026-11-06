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

The search is self-timed: the tool call blocks until the search completes,
fails, or runs out of its own budget — the harness never detaches it, and
a queued search waits for the running one instead of overlapping. Two
budgets bound the call. Starting the stealth browser from cold gets its
own generous launch budget: it is startup, not search work, and its
duration varies with OS cache state. The engine chain gets a search
budget enforced cooperatively — each navigation's timeout is capped by
what remains of the budget, so expiry stops the chain within that
navigation instead of abandoning it mid-flight.
"""

import atexit
import random
import re
import threading
import time
from base64 import urlsafe_b64decode
from urllib.parse import parse_qs, quote_plus, urlparse

from .browser import Browser
from ..config import (
    get_logger,
    load_browser_launch_timeout_s,
    load_max_cancel_wait_s,
    load_web_search_timeout_s,
)
from ..state import ToolCancelled, get_cancel_event

_URL_PATTERN = re.compile(r"^https?://\S+$", re.IGNORECASE)

_MAX_RESULTS = 10

_ENGINE_COOLDOWN_S = (15 * 60, 30 * 60)

_GOTO_TIMEOUT_S = 30

# Slack on top of the launch and search budgets for the harness's
# web_search deadline and the pacing lock's worst-case hold: the random
# pacing sleep before the chain and the margin of one bounded
# navigation. Both budgets are enforced inside the tool, so these limits
# only fire when that enforcement has failed.
WEB_SEARCH_DEADLINE_SLACK_S = 60

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

_launch_lock = threading.Lock()

_browser: Browser | None = None

_launch: "_BrowserLaunch | None" = None

_engine_blocked_until: dict[str, float] = {}


class _BrowserLaunch:
    """One in-flight construction of the shared search browser.

    The constructor runs in a helper thread; waiters gather on `ready`,
    each bounded by the launch budget. A caller that outlives the budget
    abandons its wait — the helper keeps starting the browser, so when
    it eventually lands the next call finds it warm. A constructor that
    fails records its error here for every waiter to raise.
    """

    def __init__(self):
        self.ready = threading.Event()
        self.error: BaseException | None = None


def _search_lock_wait_s() -> float:
    """Return the lock-wait cap for a queued search.

    Long enough for the running search to finish legitimately — launch
    budget, search budget, the pacing sleep ahead of it, and the
    harness's stop grace — so a queued search never fails one that would
    have succeeded. Short enough that a search wedged past every budget
    fails the waiter with a clear error instead of hanging the turn.
    """
    return (
        load_browser_launch_timeout_s()
        + load_web_search_timeout_s()
        + WEB_SEARCH_DEADLINE_SLACK_S
        + load_max_cancel_wait_s()
    )


def search_deadline_s() -> float:
    """Return the harness deadline for one web_search tool call.

    The search enforces its own budgets, so this deadline is a bug guard
    that must only fire when that enforcement has failed. It therefore
    outlasts every legitimate path: the pacing lock's wait cap — which
    already embeds the launch budget, the search budget, the pacing
    sleep, and the stop grace of the running search a queued call waits
    behind — plus the searcher's own budget and the slack for pacing and
    one bounded navigation.
    """
    return (
        _search_lock_wait_s()
        + load_web_search_timeout_s()
        + WEB_SEARCH_DEADLINE_SLACK_S
    )


def _interruptible_sleep(seconds: float) -> None:
    """Sleep for up to seconds, ending early when the call is cancelled."""
    cancel_event = get_cancel_event()
    if cancel_event is not None:
        cancel_event.wait(seconds)
    else:
        time.sleep(seconds)


def _cancelled() -> bool:
    """Whether the harness has asked the current call to stop."""
    cancel_event = get_cancel_event()
    return cancel_event is not None and cancel_event.is_set()


def _remaining_s(deadline: float) -> float:
    """Return the seconds left of the search budget."""
    return deadline - time.monotonic()


def _search_budget_error() -> ValueError:
    """The error raised when the engine chain outlives its budget."""
    return ValueError(
        "Web search timed out after"
        f" {load_web_search_timeout_s():.0f} seconds without completing."
        " The search engines were slow or unreachable; retrying usually"
        " works because the browser is already warm."
    )


def search(query, delay_s=2):
    """Return web search results for a query.

    Everything is searched through the stealth browser, trying DuckDuckGo
    (html, then lite), Bing, and Mojeek in order until one returns results.
    Up to 10 title/URL/snippet blocks come back. URLs are not queries: for
    those, the browser tool's `visit` command opens the page.

    Sleeps a randomized duration between 'delay_s' and 1.5x that before
    every search to avoid getting rate-limited; fixed waits would themselves
    be a fingerprint. The sleep and the request share a process-wide lock,
    so searches from concurrent sessions run one after another and stay at
    least 'delay_s' apart instead of firing together: a queued search waits
    for the running one, which always finishes within its budgets, instead
    of overlapping. Raises ValueError when the wait outlasts the lock's
    worst-case hold — the running search wedged past every budget.
    """
    logger = get_logger()
    wait_started = time.monotonic()
    wait_limit = _search_lock_wait_s()
    if not _search_lock.acquire(timeout=wait_limit):
        logger.warning(
            "Web search for %.100s gave up after %.0fs: another search"
            " still holds the lock past every budget",
            query,
            wait_limit,
        )
        raise ValueError(
            "Another web search is still running and has held the lock for"
            f" {wait_limit:.0f} seconds — past every budget, so it appears"
            " stuck and could not be stopped. Wait a moment, then retry."
        )
    try:
        waited = time.monotonic() - wait_started
        if waited > 0.1:
            logger.info(
                "Web search for %.100s waited %.1fs for the search lock",
                query,
                waited,
            )
        _interruptible_sleep(random.uniform(delay_s, delay_s * 1.5))
        return _search(query)
    finally:
        _search_lock.release()


def _search(query):
    query = query.strip()
    if _URL_PATTERN.match(query):
        raise ValueError(
            "URLs are not search queries. To open a page, use the browser"
            " tool with command 'visit'."
        )

    results = _browser_search(query)
    if not results:
        get_logger().warning("Web search returned no results for %.100s", query)
        return "Error performing web search"
    return _format_results(results)


def _browser_search(query):
    """Run the engine chain in the stealth browser; return result dicts.

    The browser is acquired before the budget clock starts: starting it
    from cold is startup governed by the launch budget, not search work.
    Engines that recently served a challenge page are skipped for a
    randomized cooldown: re-requesting a host that demands verification
    at search frequency is the repeat-offense pattern that hardens soft
    challenges into IP bans. The chain stops within one bounded
    navigation when the search budget runs out.
    """
    browser = _get_browser()
    deadline = time.monotonic() + load_web_search_timeout_s()
    for index, engine in enumerate(_ENGINES):
        name = engine[0]
        blocked_until = _engine_blocked_until.get(name, 0.0)
        if blocked_until > time.monotonic():
            get_logger().info(
                "Search engine '%s' is cooling down after a challenge; skipping",
                name,
            )
            continue
        if _cancelled():
            raise ToolCancelled("the harness stopped the web search at its deadline")
        if _remaining_s(deadline) <= 0:
            raise _search_budget_error()
        get_logger().info("Search engine '%s': trying %.100s", name, query)
        engine_started = time.monotonic()
        try:
            results = _search_engine(browser, query, engine, deadline)
        except ToolCancelled:
            raise
        except Exception:
            get_logger().info(
                "Search engine '%s': failed after %.1fs for query %.100s",
                name,
                time.monotonic() - engine_started,
                query,
            )
            get_logger().exception(
                "Search engine '%s' failed for query %.100s", name, query
            )
            results = []
        if results:
            get_logger().info(
                "Search engine '%s': %d results in %.1fs",
                name,
                len(results),
                time.monotonic() - engine_started,
            )
            return results
        get_logger().info(
            "Search engine '%s': no results after %.1fs",
            name,
            time.monotonic() - engine_started,
        )
        if _page_shows_challenge(browser):
            cooldown = random.uniform(*_ENGINE_COOLDOWN_S)
            _engine_blocked_until[name] = time.monotonic() + cooldown
            get_logger().info(
                "Search engine '%s' served a challenge; pausing it for %.0fs",
                name,
                cooldown,
            )
        if index < len(_ENGINES) - 1 and _remaining_s(deadline) > 0:
            _interruptible_sleep(
                min(random.uniform(0.5, 1.5), max(0.0, _remaining_s(deadline)))
            )
    return []


def _page_shows_challenge(browser: Browser) -> bool:
    """Whether the current page is a captcha/challenge wall."""
    probe = browser.run(browser.page.evaluate(_CHALLENGE_PROBE_JS))
    return _is_challenge(probe)


def _is_challenge(probe) -> bool:
    """Classify a challenge probe: a widget hit or a marker phrase."""
    return probe == "widget" or any(
        marker in probe for marker in _CHALLENGE_TEXT_MARKERS
    )


def _search_engine(browser: Browser, query, engine, deadline):
    """Search one engine and return its parsed results.

    The navigation's timeout is capped by the budget remaining, so a
    budget that expires mid-chain stops it within that navigation.
    """
    _name, url_template, referer, extract_js, unwrap = engine
    page = browser.page
    goto_timeout = min(_GOTO_TIMEOUT_S, _remaining_s(deadline))
    browser.run(
        page.goto(
            url_template.format(query=quote_plus(query)),
            wait_until="domcontentloaded",
            timeout=max(0.001, goto_timeout) * 1000,
            referer=referer,
        )
    )
    _interruptible_sleep(
        min(random.uniform(0.7, 1.5), max(0.0, _remaining_s(deadline)))
    )
    raw = browser.run(page.evaluate(extract_js))
    results = [dict(item, href=unwrap(item["href"])) for item in raw]
    return [r for r in results if r["href"]][:_MAX_RESULTS]


def _get_browser():
    """Return the shared search browser, starting it once.

    Creation is serialized and governed by the launch budget, not the
    search budget: a cold start (process spawn, fingerprint setup, cold
    OS file caches) takes seconds to minutes and is startup, not work.
    The constructor runs in a helper thread so the budget can bound the
    wait: a caller that outlives it raises cleanly while the helper
    keeps starting the browser, and when it eventually lands the next
    call finds it warm. A constructor wedged forever is a bug the
    harness's own deadline reports as a hung call.
    """
    global _browser, _launch
    if _browser is not None:
        return _browser
    launch_budget_s = load_browser_launch_timeout_s()
    started = time.monotonic()
    with _launch_lock:
        if _browser is not None:
            return _browser
        launch = _launch
        if launch is None:
            launch = _BrowserLaunch()
            _launch = launch
            threading.Thread(
                target=_construct_browser,
                args=(launch,),
                name="syna-search-browser-start",
                daemon=True,
            ).start()
    remaining = launch_budget_s - (time.monotonic() - started)
    if not launch.ready.wait(max(0.0, remaining)):
        get_logger().error(
            "The search browser did not start within %.0fs.", launch_budget_s
        )
        raise ValueError(
            f"The search browser did not start within {launch_budget_s:.0f} seconds."
        )
    if launch.error is not None:
        with _launch_lock:
            if _launch is launch:
                _launch = None
        raise launch.error
    return _browser


def _construct_browser(launch: _BrowserLaunch):
    """Build the shared search browser, publish it, and wake the waiters."""
    global _browser
    started = time.monotonic()
    try:
        browser = Browser(headless=True)
    except BaseException as exc:
        launch.error = exc
        launch.ready.set()
        return
    with _launch_lock:
        if _browser is None:
            _browser = browser
            atexit.register(_close_browser)
    launch.ready.set()
    get_logger().info("Search browser started in %.1fs.", time.monotonic() - started)


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
