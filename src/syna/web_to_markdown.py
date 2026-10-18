"""Headless-browser page fetcher that renders JavaScript pages and converts
the DOM to Markdown.

The page is loaded with DrissionPage the way a person would: random pauses,
cookie-banner dismissal, gradual scrolling for lazy-loading and a wait for
automatic anti-bot challenges to clear. The rendered HTML is cleaned,
links and images are absolutized, and the main content node is converted
to Markdown with markdownify.

Runnable as a CLI: python -m syna.web_to_markdown <url> [options]
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment
from DrissionPage import ChromiumOptions, ChromiumPage
from markdownify import markdownify as html_to_md

log = logging.getLogger("web_to_markdown")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BLOCK_MARKERS = (
    "just a moment",
    "attention required",
    "verify you are human",
    "checking your browser",
    "access denied",
    "un momento",
    "verificando que eres humano",
    "enable javascript and cookies",
)

ALWAYS_REMOVE = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
    "link",
    "meta",
)
CHROME_REMOVE = ("nav", "footer", "aside", "header", "form")


class FetchError(RuntimeError):
    """Raised when fetching or converting a page fails."""


@dataclass
class Config:
    headless: bool = True
    timeout: float = 30.0
    retries: int = 3
    backoff: float = 3.0
    user_agent: str = DEFAULT_UA
    lang: str = "en-US"
    max_scroll_steps: int = 40
    challenge_wait: float = 20.0
    full_page: bool = False
    include_images: bool = True
    browser_path: str | None = None


def _build_options(cfg: Config) -> ChromiumOptions:
    co = ChromiumOptions()
    co.auto_port()
    co.headless(cfg.headless)
    co.set_user_agent(cfg.user_agent)
    co.set_argument(f"--lang={cfg.lang}")
    co.set_argument("--window-size=1366,768")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    if sys.platform.startswith("linux"):
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-dev-shm-usage")
    if cfg.browser_path:
        co.set_browser_path(cfg.browser_path)
    return co


def _pause(a: float, b: float) -> None:
    time.sleep(random.uniform(a, b))


def _body_text(page: ChromiumPage) -> str:
    try:
        return page.run_js("return document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def _is_blocked(page: ChromiumPage) -> bool:
    head = ((page.title or "") + " " + _body_text(page)[:600]).lower()
    return any(m in head for m in BLOCK_MARKERS)


def _wait_past_challenge(page: ChromiumPage, max_wait: float) -> None:
    """Wait for automatic challenges (e.g. Cloudflare) to disappear."""
    if not _is_blocked(page):
        return
    log.warning("Possible challenge/block page; waiting up to %.0fs...", max_wait)
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        time.sleep(1.0)
        if not _is_blocked(page):
            log.info("Challenge cleared.")
            _pause(0.5, 1.2)
            return
    raise FetchError(
        "The page still shows an anti-bot challenge/block. "
        "Try --no-headless or solve it manually."
    )


def _dismiss_cookie_banner(page: ChromiumPage) -> None:
    js = """
    const re = /^(accept( all)?|allow all|agree|got it|i agree|aceptar( todo| todas)?|acepto|entendido|de acuerdo|permitir todo)\\b/i;
    const els = [...document.querySelectorAll('button,a[role=button],[role=button],input[type=button]')];
    const b = els.find(e => re.test(((e.innerText || e.value) || '').trim()));
    if (b) { b.click(); return true; }
    return false;
    """
    try:
        if page.run_js(js):
            log.debug("Cookie banner dismissed.")
            _pause(0.4, 0.9)
    except Exception:
        pass


def _human_scroll(page: ChromiumPage, max_steps: int) -> None:
    """Scroll gradually with random pauses to trigger lazy-loading."""
    try:
        height = page.run_js("return document.documentElement.scrollHeight") or 0
        pos, steps = 0, 0
        while steps < max_steps:
            step = random.randint(300, 700)
            pos += step
            page.run_js(f"window.scrollTo({{top: {pos}, behavior: 'smooth'}});")
            _pause(0.25, 0.8)
            steps += 1
            height = (
                page.run_js("return document.documentElement.scrollHeight") or height
            )
            if pos >= height:
                break
        _pause(0.4, 0.9)
        page.run_js("window.scrollTo({top: 0, behavior: 'smooth'});")
        _pause(0.4, 0.8)
    except Exception as exc:
        log.debug("Scroll interrupted: %s", exc)


def _wait_dom_stable(page: ChromiumPage, max_wait: float = 8.0) -> None:
    """Wait until the visible text stops changing (dynamic content)."""
    deadline = time.monotonic() + max_wait
    last, stable = -1, 0
    while time.monotonic() < deadline and stable < 3:
        size = len(_body_text(page))
        stable = stable + 1 if size == last else 0
        last = size
        time.sleep(0.5)


def _load_rendered_html(url: str, cfg: Config) -> tuple[str, str, str]:
    """Open the browser, load the URL and return (html, final_url, title)."""
    page = ChromiumPage(addr_or_opts=_build_options(cfg))
    try:
        page.set.timeouts(base=10, page_load=cfg.timeout)
        _pause(0.3, 0.8)
        ok = page.get(url, retry=1, interval=1, timeout=cfg.timeout)
        if ok is False:
            raise FetchError(f"Could not load the page: {url}")
        try:
            page.wait.doc_loaded(timeout=cfg.timeout)
        except Exception:
            log.debug("doc_loaded timed out; continuing with what was loaded.")

        _pause(0.8, 1.6)
        _wait_past_challenge(page, cfg.challenge_wait)
        _dismiss_cookie_banner(page)
        _human_scroll(page, cfg.max_scroll_steps)
        _wait_dom_stable(page)

        html = page.html
        if not html or len(html) < 200:
            raise FetchError("The fetched HTML is empty or too short.")
        return html, page.url or url, page.title or ""
    finally:
        try:
            page.quit()
        except Exception:
            pass


def _absolutize(soup: BeautifulSoup, base_url: str, include_images: bool) -> None:
    base_tag = soup.find("base", href=True)
    base = urljoin(base_url, base_tag["href"]) if base_tag else base_url

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "data:", "mailto:", "tel:", "#")):
            if href.startswith("javascript:") or href.startswith("data:"):
                del a["href"]
            continue
        a["href"] = urljoin(base, href)

    for img in soup.find_all("img"):
        if not include_images:
            img.decompose()
            continue
        src = (img.get("src") or "").strip()
        if not src or src.startswith("data:"):
            for attr in ("data-src", "data-original", "data-lazy-src"):
                cand = (img.get(attr) or "").strip()
                if cand and not cand.startswith("data:"):
                    src = cand
                    break
            else:
                srcset = (img.get("srcset") or img.get("data-srcset") or "").strip()
                src = srcset.split(",")[0].strip().split(" ")[0] if srcset else ""
        if not src or src.startswith("data:"):
            img.decompose()
            continue
        img["src"] = urljoin(base, src)


def _pick_main_node(soup: BeautifulSoup):
    for sel in ("main", "article", "[role=main]", "#content", "#main"):
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            return node
    return soup.body or soup


def html_to_markdown(
    html: str,
    base_url: str,
    title: str = "",
    *,
    full_page: bool = False,
    include_images: bool = True,
) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for tag in soup(ALWAYS_REMOVE):
        tag.decompose()
    for tag in soup.select("[hidden], [aria-hidden=true]"):
        tag.decompose()

    if not full_page:
        for tag in soup(CHROME_REMOVE):
            tag.decompose()

    _absolutize(soup, base_url, include_images)
    node = soup.body or soup if full_page else _pick_main_node(soup)

    text = html_to_md(
        str(node),
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
    )

    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise FetchError("Markdown conversion produced empty content.")

    if title and not re.match(r"^#\s", text):
        text = f"# {title.strip()}\n\n{text}"
    return text + "\n"


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid URL: {url!r}")
    return url


def fetch_markdown(url: str, cfg: Config | None = None) -> str:
    """Load `url` the way a person would and return its content as Markdown."""
    cfg = cfg or Config()
    url = _normalize_url(url)

    last_exc: Exception | None = None
    for attempt in range(1, cfg.retries + 1):
        try:
            log.info("Attempt %d/%d: %s", attempt, cfg.retries, url)
            html, final_url, title = _load_rendered_html(url, cfg)
            return html_to_markdown(
                html,
                final_url,
                title,
                full_page=cfg.full_page,
                include_images=cfg.include_images,
            )
        except Exception as exc:
            last_exc = exc
            log.warning("Attempt %d failed: %s", attempt, exc)
            if attempt < cfg.retries:
                time.sleep(cfg.backoff * attempt + random.uniform(0, 1.5))

    raise FetchError(
        f"Could not fetch {url} after {cfg.retries} attempts: {last_exc}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Web -> Markdown with DrissionPage")
    p.add_argument("url")
    p.add_argument("-o", "--output", help="Output file (defaults to stdout)")
    p.add_argument("--no-headless", action="store_true", help="Show the browser")
    p.add_argument(
        "--full-page", action="store_true", help="Do not filter nav/footer/aside"
    )
    p.add_argument("--no-images", action="store_true", help="Skip images")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--browser-path", help="Path to the Chrome/Chromium executable")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    cfg = Config(
        headless=not args.no_headless,
        timeout=args.timeout,
        retries=args.retries,
        full_page=args.full_page,
        include_images=not args.no_images,
        browser_path=args.browser_path,
    )

    try:
        markdown = fetch_markdown(args.url, cfg)
    except (FetchError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(markdown)
        log.info("Saved to %s (%d characters)", args.output, len(markdown))
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
