"""
camoufox_harness.py - persistent stealth browser session exposed as LLM tools.

Core idea: instead of sending HTML, run a walker inside the page that emits a
compact, line-based "accessibility-ish" view:

    URL: https://shop.example/p/42
    Title: Blue Widget
    Scroll: 0/5400px (viewport 900)
    <header>
      [1] link "Home" -> /
      [2] input:search "Search" value=""
      [3] button "Go"
    <main>
    # Blue Widget
    Price: $19.99 In stock
    [4] select "Size" value="M" options=[S|M|L]
    [5] button "Add to cart"

Everything that is not visible text or an interactable element is dropped
(scripts, styles, svg, hidden nodes, empty wrappers, attributes, classes...).
Interactables get a stable numeric ref that the model passes back to click/type/etc.

Usage:
    async with BrowserSession() as s:
        print(await s.call("goto", {"url": "https://example.com"}))
        print(await s.call("click", {"ref": 1}))
"""

from __future__ import annotations

import asyncio
import difflib  # noqa: F401  (kept for easy swap to a smarter differ)
import random
import sys
import threading
from typing import Any

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Error as PWError

# --------------------------------------------------------------------------- #
# In-page snapshot walker
# --------------------------------------------------------------------------- #
# Camoufox runs page.evaluate() in an isolated world by default (prefix a
# script with "mw:" to run in the page's main world), so the state below
# (window.__aiState) is invisible to the site's own JS. Verify on your version;
# if state does not persist between calls, refs will report "not found" and the
# model will just re-snapshot.

SNAPSHOT_JS = r"""
(opts) => {
  const S = (window.__aiState = window.__aiState || { ids: new WeakMap(), els: new Map(), next: 1 });
  for (const [id, el] of S.els) if (!el.isConnected) S.els.delete(id);

  const vh = window.innerHeight, vw = window.innerWidth;
  const MAXLINE = opts.maxLine || 240;
  const SKIP = new Set(['script','style','noscript','template','head','meta','link','svg','canvas',
                        'audio','video','object','embed','map','datalist','option','optgroup']);
  const ROLES = new Set(['button','link','checkbox','radio','tab','menuitem','menuitemcheckbox',
                         'menuitemradio','option','switch','combobox','textbox','searchbox',
                         'slider','spinbutton','treeitem']);
  const LANDMARK = { nav:'nav', main:'main', header:'header', footer:'footer', aside:'aside',
                     form:'form', dialog:'dialog', table:'table' };
  const ROLE_LANDMARK = { navigation:'nav', main:'main', dialog:'dialog', alertdialog:'dialog',
                          banner:'header', contentinfo:'footer', complementary:'aside', search:'search' };
  const SEMANTIC = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link]';

  const squash = s => (s || '').replace(/\s+/g, ' ').trim();
  const cut = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
  const inView = r => r.bottom >= -50 && r.top <= vh + 50 && r.right >= 0 && r.left <= vw;

  function idOf(el) {
    let id = S.ids.get(el);
    if (!id) { id = S.next++; S.ids.set(el, id); }
    S.els.set(id, el);
    return id;
  }

  function nameOf(el) {
    const tag = el.localName;
    let n = squash(el.getAttribute('aria-label'));
    if (!n) {
      const lb = el.getAttribute('aria-labelledby');
      if (lb) n = squash(lb.split(/\s+/).map(i => (document.getElementById(i) || {}).textContent || '').join(' '));
    }
    if (n) return n;
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      if (el.labels && el.labels.length) n = squash(el.labels[0].textContent);
      if (!n && tag === 'input' && /^(submit|button|reset)$/.test(el.type)) n = squash(el.value);
      if (!n) n = squash(el.getAttribute('placeholder')) || squash(el.getAttribute('title')) || squash(el.name);
      return n;
    }
    n = squash(el.innerText != null ? el.innerText : el.textContent);
    if (!n) { const img = el.querySelector('img[alt]'); if (img) n = squash(img.alt); }
    if (!n) n = squash(el.getAttribute('title'));
    if (!n) { const t = el.querySelector('svg title'); if (t) n = squash(t.textContent); }
    return n;
  }

  function kindOf(el, cs) {
    const tag = el.localName, role = el.getAttribute('role');
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'hidden') return null;
      if (/^(submit|button|reset|image)$/.test(t)) return 'button';
      if (t === 'checkbox' || t === 'radio') return t;
      return 'input:' + t;
    }
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textarea';
    if (tag === 'summary') return 'summary';
    if (role && ROLES.has(role)) return role;
    const ce = el.getAttribute('contenteditable');
    if (ce !== null && ce !== 'false') return 'editable';
    // generic elements: onclick / cursor:pointer (outermost only, and not wrappers of real controls)
    if (el.hasAttribute('onclick') || (cs.cursor === 'pointer' && tag !== 'label' || (tag === 'label' && cs.cursor === 'pointer'))) {
      const p = el.parentElement;
      if (!el.hasAttribute('onclick') && p && getComputedStyle(p).cursor === 'pointer') return null;
      if (el.querySelector(SEMANTIC)) return null;
      return 'clickable';
    }
    return null;
  }

  function describe(el, kind) {
    const tag = el.localName;
    let s = `[${idOf(el)}] ${kind}`;
    const nm = cut(nameOf(el), 80);
    if (nm) s += ` "${nm}"`;
    if (kind === 'link') {
      try {
        const u = new URL(el.href, location.href);
        s += ' -> ' + cut(u.origin === location.origin ? u.pathname + u.search + u.hash : u.href, 70);
      } catch (e) {}
    } else if (tag === 'input' && (kind === 'checkbox' || kind === 'radio')) {
      s += el.checked ? ' checked' : ' unchecked';
    } else if (tag === 'input' || tag === 'textarea') {
      const v = el.type === 'password' ? (el.value ? '••••' : '') : cut(el.value || '', 60);
      s += ` value="${v}"`;
    } else if (tag === 'select') {
      const opts = Array.from(el.options).slice(0, 15).map(o => squash(o.text));
      const sel = el.selectedOptions[0];
      s += ` value="${sel ? squash(sel.text) : ''}" options=[${opts.join('|')}${el.options.length > 15 ? '|…' : ''}]`;
    } else if (el.getAttribute('aria-checked')) {
      s += el.getAttribute('aria-checked') === 'true' ? ' checked' : ' unchecked';
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') s += ' disabled';
    const ex = el.getAttribute('aria-expanded');
    if (ex) s += ex === 'true' ? ' expanded' : ' collapsed';
    if (el.getAttribute('aria-selected') === 'true') s += ' selected';
    return s;
  }

  const lines = [];
  let buf = [];
  let depth = 0;

  function flush() {
    const parts = buf;
    buf = [];
    let out = '', n = 0, clipped = false;
    for (const p of parts) {
      if (typeof p === 'object') { out += p.t; continue; }       // ref tokens are never cut or dropped
      if (n >= MAXLINE) {
        if (!clipped && p.trim()) { out += '…'; clipped = true; }
        continue;
      }
      const room = MAXLINE - n;
      if (p.length > room) { out += p.slice(0, room) + '…'; clipped = true; n = MAXLINE; }
      else { out += p; n += p.length; }
    }
    const t = squash(out);
    if (!t || /^(-|#+|\|)$/.test(t)) return;
    const line = '  '.repeat(depth) + t;
    if (lines[lines.length - 1] !== line) lines.push(line);
  }

  function kids(el) {
    if (el.localName === 'slot') {
      const a = el.assignedNodes({ flatten: true });
      return a.length ? a : el.childNodes;
    }
    return el.shadowRoot ? el.shadowRoot.childNodes : el.childNodes;
  }

  function walk(node) {
    if (node.nodeType === 3) { buf.push(node.nodeValue); return; }
    if (node.nodeType !== 1) return;
    const el = node, tag = el.localName;
    if (SKIP.has(tag)) return;
    if (el.getAttribute('aria-hidden') === 'true') return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    if (cs.opacity === '0') return;

    const isCell = tag === 'td' || tag === 'th';
    const block = !/^inline/.test(cs.display) && cs.display !== 'contents' && !isCell;

    // <label> whose visible control already uses its text as its name: don't print it twice
    if (tag === 'label' && el.control) {
      const c = el.control, cr = c.getBoundingClientRect(), ccs = getComputedStyle(c);
      if (c.type !== 'hidden' && cr.width > 0 && cr.height > 0 &&
          ccs.display !== 'none' && ccs.visibility !== 'hidden') {
        if (!el.contains(c)) return;                       // <label for=...>: text lives on the control
        if (block) flush();
        for (const k of kids(el)) if (k.nodeType === 1) walk(k);   // nested control: skip bare text nodes
        if (block) flush();
        return;
      }
    }

    const kind = kindOf(el, cs);
    if (kind) {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return;
      if (opts.viewportOnly && !inView(r)) return;
      if (block) flush();
      buf.push({ t: ' ' + describe(el, kind) + ' ' });
      if (block) flush();
      return;                       // do not descend: label already holds the text
    }

    if (tag === 'img') {
      const alt = squash(el.getAttribute('alt'));
      if (alt) buf.push(` [img: ${cut(alt, 60)}] `);
      return;
    }
    if (tag === 'br') { flush(); return; }
    if (tag === 'iframe' || tag === 'frame') {
      flush();
      lines.push('  '.repeat(depth) + `(iframe ${cut(el.getAttribute('src') || '', 80)})`);
      return;
    }

    const role = el.getAttribute('role');
    const lm = LANDMARK[tag] || ROLE_LANDMARK[role];
    const hm = /^h([1-6])$/.exec(tag);

    if (block) flush();
    if (isCell && buf.some(p => typeof p === 'object' || p.trim())) buf.push(' | ');
    if (hm) buf.push('#'.repeat(+hm[1]) + ' ');
    else if (tag === 'li') buf.push('- ');

    let mark = -1;
    if (lm) {
      flush();
      mark = lines.length;
      lines.push('  '.repeat(depth) + `<${lm}>`);
      depth++;
    }
    for (const c of kids(el)) walk(c);
    if (lm) {
      flush();
      depth--;
      if (lines.length === mark + 1) lines.pop();   // empty landmark -> drop it
    }
    if (block || tag === 'tr') flush();
  }

  walk(document.body || document.documentElement);
  flush();

  const de = document.documentElement;
  return { lines, scrollY: Math.round(window.scrollY), scrollH: de.scrollHeight, vh };
}
"""


class Browser:
    def __init__(self, **session_kwargs: Any):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self.last: str = ""
        self._s: BrowserSession = self.run(BrowserSession(**session_kwargs).start())

    # -- plumbing ----------------------------------------------------------- #
    def run(self, coro):
        """
        Run any coroutine on the browser's loop,
        e.g. b.run(b.page.screenshot(path='a.png')).
        """
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _tool(self, name: str, **args: Any) -> None:
        args = {k: v for k, v in args.items() if v is not None}
        self.last = self.run(self._s.call(name, args))
        return self.last

    @property
    def page(self):
        """The live Playwright page (its methods are async: wrap with b.run(...))."""
        return self._s.page

    # -- tools -------------------------------------------------------------- #
    def goto(self, url: str):
        return self._tool("goto", url=url)

    def snapshot(self, viewport_only: bool = False):
        return self._tool("snapshot", viewport_only=viewport_only)

    def click(self, ref: int):
        return self._tool("click", ref=ref)

    def type(self, ref: int, text: str, clear: bool = True, submit: bool = False):
        return self._tool("type", ref=ref, text=text, clear=clear, submit=submit)

    def select(self, ref: int, option: str):
        return self._tool("select", ref=ref, option=option)

    def press(self, key: str):
        return self._tool("press", key=key)

    def scroll(self, direction: str = "down", amount: float = 0.8):
        return self._tool("scroll", direction=direction, amount=amount)

    def back(self):
        return self._tool("back")

    def read(self, ref: int | None = None, max_chars: int = 6000):
        return self._tool("read", ref=ref, max_chars=max_chars)

    def close(self) -> None:
        self.run(self._s.close())
        self._loop.call_soon_threadsafe(self._loop.stop)
        return "closed"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def detect_host_os() -> str:
    """Camoufox `os` value matching the machine we're actually running on."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


class ToolError(Exception):
    """Raised for errors that should be shown to the model verbatim."""


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
class BrowserSession:
    def __init__(
        self,
        *,
        headless: bool | str = True,  # True | False | "virtual" (Linux)
        humanize: bool | float = True,  # camoufox human-like cursor movement
        user_data_dir: str
        | None = None,  # set -> persistent profile (cookies survive restarts)
        proxy: dict | None = None,
        geoip: bool = False,
        max_chars: int = 12000,  # hard cap on any snapshot (~3k tokens)
        **camoufox_kwargs: Any,
    ):
        self.max_chars = max_chars
        # Pin the fingerprint OS to the real host (random OS = inconsistent fingerprint).
        # Override with os="macos"/..., or os=None to let Camoufox pick randomly.
        camoufox_kwargs.setdefault("os", detect_host_os())
        self._kw = dict(
            headless=headless, humanize=humanize, geoip=geoip, **camoufox_kwargs
        )
        if proxy:
            self._kw["proxy"] = proxy
        self._persistent = user_data_dir is not None
        if self._persistent:
            self._kw.update(persistent_context=True, user_data_dir=user_data_dir)
        self._cm = None
        self.context = None
        self.page = None
        self._last: tuple[str, list[str]] | None = None

    # -- lifecycle ---------------------------------------------------------- #
    async def start(self) -> "BrowserSession":
        self._cm = AsyncCamoufox(**self._kw)
        obj = await self._cm.__aenter__()
        if self._persistent:
            self.context = obj
            self.page = obj.pages[0] if obj.pages else await obj.new_page()
        else:
            self.page = await obj.new_page()
            self.context = self.page.context
        self.context.on("page", self._on_new_page)
        return self

    async def close(self) -> None:
        if self._cm:
            await self._cm.__aexit__(None, None, None)
            self._cm = None

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.close()

    def _on_new_page(self, p) -> None:
        # a click opened a new tab -> follow it
        self.page = p
        self._last = None

    def _live_page(self):
        if self.page.is_closed():
            pages = [p for p in self.context.pages if not p.is_closed()]
            if not pages:
                raise ToolError("all tabs are closed")
            self.page = pages[-1]
        return self.page

    # -- dispatch ----------------------------------------------------------- #
    async def call(self, name: str, args: dict | None = None) -> str:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return f"ERROR: unknown tool '{name}'"
        try:
            return await fn(**(args or {}))
        except ToolError as e:
            return f"ERROR: {e}"
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"
        except PWError as e:
            return "ERROR: " + str(e).strip().splitlines()[0][:300]

    # -- snapshot ----------------------------------------------------------- #
    async def _raw_snapshot(self, viewport_only: bool) -> dict:
        page = self._live_page()
        for attempt in (0, 1):
            try:
                return await page.evaluate(
                    SNAPSHOT_JS, {"viewportOnly": viewport_only, "maxLine": 240}
                )
            except PWError as e:
                # navigation in flight -> context destroyed; settle and retry once
                if attempt == 0 and "context" in str(e).lower():
                    await self._settle()
                    continue
                raise

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_chars:
            return text
        cut_at = text.rfind("\n", 0, self.max_chars)
        dropped = text[cut_at:].count("\n")
        return (
            text[:cut_at]
            + f"\n... [{dropped} more lines truncated; scroll, use viewport_only, or read(ref)]"
        )

    async def _observe(self, *, viewport_only: bool = False, diff: bool = False) -> str:
        data = await self._raw_snapshot(viewport_only)
        page = self._live_page()
        url, lines = page.url, data["lines"]
        head = (
            f"URL: {url}\nTitle: {await page.title()}\n"
            f"Scroll: {data['scrollY']}/{data['scrollH']}px (viewport {data['vh']})\n"
        )
        prev, self._last = self._last, (url, lines)

        if diff and prev and prev[0] == url:
            old = set(prev[1])
            new = [l for l in lines if l not in old]
            removed = len(old - set(lines))
            if len(new) < 0.6 * max(len(lines), 1):
                if not new and not removed:
                    return head + "(no visible changes)"
                body = "\n".join("+ " + l.strip() for l in new)
                return self._truncate(
                    head
                    + f"(changes since last view: {len(new)} added, {removed} removed; "
                    f"call snapshot for the full page)\n{body}"
                )
        return self._truncate(head + "\n".join(lines))

    async def _settle(self) -> None:
        page = self._live_page()
        for state, t in (("domcontentloaded", 5000), ("networkidle", 2000)):
            try:
                await page.wait_for_load_state(state, timeout=t)
            except PWError:
                pass
        await asyncio.sleep(0.25)

    async def _el(self, ref: int):
        page = self._live_page()
        h = await page.evaluate_handle(
            "(id) => (window.__aiState && window.__aiState.els.get(id)) || null",
            int(ref),
        )
        el = h.as_element()
        if el is None:
            raise ToolError(
                f"ref {ref} not found (page changed?). Call snapshot to refresh refs."
            )
        return el

    # -- tools -------------------------------------------------------------- #
    async def tool_goto(self, url: str) -> str:
        page = self._live_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await self._settle()
        self._last = None
        return await self._observe()

    async def tool_snapshot(self, viewport_only: bool = False) -> str:
        return await self._observe(viewport_only=viewport_only)

    async def tool_click(self, ref: int) -> str:
        el = await self._el(ref)
        await el.scroll_into_view_if_needed(timeout=5000)
        await el.click(timeout=7000)
        await self._settle()
        return await self._observe(diff=True)

    async def tool_type(
        self, ref: int, text: str, clear: bool = True, submit: bool = False
    ) -> str:
        el = await self._el(ref)
        await el.scroll_into_view_if_needed(timeout=5000)
        await el.click(timeout=7000)
        if clear:
            await el.fill("")
        kb = self._live_page().keyboard
        for ch in text:  # click() above focused the element; jittered per-key timing
            await kb.type(ch)
            await asyncio.sleep(random.uniform(0.03, 0.12))
        if submit:
            await self._live_page().keyboard.press("Enter")
            await self._settle()
        return await self._observe(diff=True)

    async def tool_select(self, ref: int, option: str) -> str:
        el = await self._el(ref)
        try:
            await el.select_option(label=option, timeout=3000)
        except PWError:
            await el.select_option(value=option, timeout=3000)
        await self._settle()
        return await self._observe(diff=True)

    async def tool_press(self, key: str) -> str:
        await self._live_page().keyboard.press(key)
        await self._settle()
        return await self._observe(diff=True)

    async def tool_scroll(self, direction: str = "down", amount: float = 0.8) -> str:
        page = self._live_page()
        if direction == "top":
            await page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            await page.evaluate(
                "window.scrollTo(0, document.documentElement.scrollHeight)"
            )
        elif direction in ("down", "up"):
            vh = await page.evaluate("window.innerHeight")
            await page.mouse.wheel(
                0, (1 if direction == "down" else -1) * int(vh * amount)
            )
        else:
            raise ToolError("direction must be down|up|top|bottom")
        await asyncio.sleep(0.5)
        return await self._observe(viewport_only=True)

    async def tool_back(self) -> str:
        await self._live_page().go_back(wait_until="domcontentloaded", timeout=15000)
        await self._settle()
        self._last = None
        return await self._observe()

    async def tool_read(self, ref: int | None = None, max_chars: int = 6000) -> str:
        """Full untruncated text of one element (or the whole page) for extraction."""
        if ref is None:
            text = await self._live_page().inner_text("body")
        else:
            text = await (await self._el(ref)).inner_text()
        text = "\n".join(l.strip() for l in text.splitlines() if l.strip())
        return text[:max_chars] + ("\n... [truncated]" if len(text) > max_chars else "")


# --------------------------------------------------------------------------- #
# Tool schemas (Anthropic format; for OpenAI wrap as {"type":"function","function":{...,"parameters":input_schema}})
# --------------------------------------------------------------------------- #
def _t(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": desc,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required or [],
        },
    }


TOOLS = [
    _t(
        "goto",
        "Open a URL in the persistent browser. Returns a compact page view. "
        'Elements like [12] button "Buy" are interactable; use the number as `ref`.',
        {"url": {"type": "string"}},
        ["url"],
    ),
    _t(
        "snapshot",
        "Re-read the current page. Set viewport_only to see just what is on screen.",
        {"viewport_only": {"type": "boolean"}},
    ),
    _t(
        "click",
        "Click the element with this ref. Returns what changed on the page.",
        {"ref": {"type": "integer"}},
        ["ref"],
    ),
    _t(
        "type",
        "Type text into an input/textarea by ref (clears first by default). "
        "submit=true presses Enter afterwards.",
        {
            "ref": {"type": "integer"},
            "text": {"type": "string"},
            "clear": {"type": "boolean"},
            "submit": {"type": "boolean"},
        },
        ["ref", "text"],
    ),
    _t(
        "select",
        "Choose an option (by visible label or value) in a <select> by ref.",
        {"ref": {"type": "integer"}, "option": {"type": "string"}},
        ["ref", "option"],
    ),
    _t(
        "press",
        "Press a keyboard key, e.g. Enter, Escape, Tab, ArrowDown.",
        {"key": {"type": "string"}},
        ["key"],
    ),
    _t(
        "scroll",
        "Scroll the page and return the visible viewport.",
        {
            "direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]},
            "amount": {
                "type": "number",
                "description": "fraction of viewport height, default 0.8",
            },
        },
    ),
    _t("back", "Navigate back in history.", {}),
    _t(
        "read",
        "Get the full plain text of one element (or the whole page if ref omitted). "
        "Use when the snapshot truncated or shortened text you need.",
        {"ref": {"type": "integer"}, "max_chars": {"type": "integer"}},
    ),
]


if __name__ == "__main__":

    async def demo():
        async with BrowserSession(headless=False) as s:
            print(await s.call("goto", {"url": "https://news.ycombinator.com"}))
            print(await s.call("click", {"ref": 5}))

    asyncio.run(demo())
