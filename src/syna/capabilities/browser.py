"""
browser.py - persistent stealth browser session exposed as LLM tools.

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

Camoufox runs page.evaluate() in an isolated world by default (prefix a
script with "mw:" to run in the page's main world), so the snapshot state
(window.__aiState) is invisible to the site's own JS. Verify on your
version: if state does not persist between calls, refs will report
"not found" and the model will just re-snapshot.

Usage:
    async with BrowserSession() as s:
        print(await s.call("visit", {"url": "https://example.com"}))
        print(await s.call("click", {"ref": 1}))
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
import threading
import time
from typing import Any
from urllib.parse import urlparse

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Error as PWError

from . import net_guard
from ..config import get_logger

ALLOWED_SCHEMES = {"http", "https"}

# Ad/identity script hosts that uBlock blocks, which makes Fandom's own error
# handler wipe the whole document and show its report.error-report.com modal
# ("Failed to load website due to adblock"). Fulfilling those requests with
# empty JavaScript keeps pages intact: the bootstrapper sees success and the
# site renders normally. Matched on the parsed hostname (wildcards are
# subdomain suffixes) and, when given, a path prefix — never on the raw URL,
# so a lookalike string inside a query cannot neutralize an innocent page.
NEUTRALIZED_SCRIPT_HOSTS = (
    ("fundingchoicesmessages.google.com", ""),
    ("*.id5-sync.com", ""),
    ("*.criteo.net", ""),
    ("*.aforearound.com", ""),
    ("script.wikia.nocookie.net", "/fandom-ae-assets/identity-service/"),
)

# A popup is only adopted as the current page within this window after a
# user-initiated interaction (click/type/submit); anything else is
# malvertising and gets closed.
POPUP_ADOPT_WINDOW_S = 10

# Body-text phrases that identify an anti-bot interstitial (Cloudflare and
# friends). Ported from the retired DrissionPage fetcher: while any marker
# is present, visit waits instead of snapshotting the challenge page.
CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "verify you are human",
    "checking your browser",
    "access denied",
    "un momento",
    "verificando que eres humano",
    "enable javascript and cookies",
)

# Overlay discovery shared by the snapshot walker, read()'s warning and
# overlay dismissal. Two independent nets: the semantic net catches
# native dialogs, Bootstrap-style modals and role=dialog surfaces (through
# open shadow roots and same-origin iframes); the veil net catches
# hand-rolled consent walls whatever their markup — a visible
# position:fixed element covering almost the whole viewport, stacking
# above the page (z-index >= 10), carrying an actionable control, and not
# containing the page's main content (so SPA app shells are not mistaken
# for walls; pages without main/article lean on the z-index guard).
# Read-only observation throughout. Returns the outermost overlay per
# subtree.
_OVERLAY_ROOTS_FN = r"""
function __synaOverlayRoots() {
  const vw = window.innerWidth, vh = window.innerHeight;
  const visible = (el) => {
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' &&
      cs.opacity !== '0' && el.getClientRects().length > 0;
  };
  const found = [];
  const frames = [document];
  for (const f of document.querySelectorAll('iframe')) {
    try { if (f.contentDocument) frames.push(f.contentDocument); } catch (e) {}
  }
  for (const doc of frames) {
    const scan = (root) => {
      for (const ov of root.querySelectorAll(
          'dialog[open], .modal.show, [role=dialog], [role=alertdialog]')) {
        if (visible(ov)) found.push(ov);
      }
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) scan(el.shadowRoot);
      }
    };
    scan(doc);
  }
  const covers = (el) => {
    const r = el.getBoundingClientRect();
    return (r.width * r.height) / Math.max(1, vw * vh) >= 0.9;
  };
  const hasControl = (el) =>
    !!el.querySelector('button, a[href], input:not([type=hidden]), select, [role=button]');
  const sampled = new Set();
  for (const doc of frames) {
    for (const [fx, fy] of [[.5, .5], [.25, .25], [.75, .25], [.25, .75], [.75, .75]]) {
      let hit = doc.elementFromPoint(vw * fx, vh * fy);
      const seen = new Set();
      while (hit && !seen.has(hit)) {
        seen.add(hit);
        sampled.add(hit);
        const sr = hit.shadowRoot;
        hit = sr ? sr.elementFromPoint(vw * fx, vh * fy) : null;
      }
    }
  }
  for (const start of sampled) {
    let node = start;
    while (node && node !== document.body && node !== document.documentElement) {
      const cs = getComputedStyle(node);
      const z = parseInt(cs.zIndex, 10);
      const main = node.ownerDocument.querySelector('main, [role=main], article');
      if (cs.position === 'fixed' && z >= 10 && covers(node) &&
          !(main && node.contains(main)) && hasControl(node) && visible(node)) {
        found.push(node);
      }
      node = node.parentElement;
    }
  }
  return [...new Set(found)].filter(
    o => !found.some(o2 => o2 !== o && o2.contains(o))
  );
}
"""

# Blocking overlays: consent banners, cookie dialogs, Bootstrap-style
# modals (including ones like justETF's country warning, which render
# inside shadow roots where document.querySelectorAll cannot reach and
# sit transparent over the page intercepting every click) and hand-rolled
# full-page walls caught by the veil rule. Dismissal clicks
# consent-language buttons outright, else the overlay's own dismiss
# control ([data-bs-dismiss], .btn-close, Close/Cancel) — never
# a "Save"/"OK"/submit button, which could confirm something on the
# user's behalf.
OVERLAY_DISMISS_JS = (
    r"""() => {"""
    + _OVERLAY_ROOTS_FN
    + r"""
  const consent = /^(accept( all)?|allow all|allow cookies|agree|got it|i agree|aceptar( todo| todas)?|acepto|entendido|de acuerdo|permitir todo)\b/i;
  const back = /^(close|cancel|dismiss|no thanks|no, thanks|×|✕)$/i;
  const visible = (el) => {
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' &&
      cs.opacity !== '0' && el.getClientRects().length > 0;
  };
  let dismissed = 0;
  for (const ov of __synaOverlayRoots()) {
    const controls = [...ov.querySelectorAll(
      'button, a[role=button], [role=button], [data-bs-dismiss], .btn-close')];
    const pick =
      controls.find(
        c => visible(c) && consent.test((c.innerText || c.value || '').trim())
      ) ||
      controls.find(c => c.matches('[data-bs-dismiss], .btn-close') && visible(c)) ||
      controls.find(c => back.test((c.innerText || '').trim()));
    if (pick) { pick.click(); dismissed++; }
  }
  return dismissed;
}"""
)

# Names of visible overlays with the same blocking classification the
# snapshot walker uses (:modal native dialogs, .modal.show, aria-modal or
# covering at least half the viewport). read() prepends a warning when
# the text it returns sits under a blocking one.
BLOCKING_OVERLAYS_JS = (
    r"""() => {"""
    + _OVERLAY_ROOTS_FN
    + r"""
  return __synaOverlayRoots()
    .map(ov => {
      const r = ov.getBoundingClientRect();
      const vw = window.innerWidth, vh = window.innerHeight;
      let blocking = ov.classList.contains('modal') ||
        ov.getAttribute('aria-modal') === 'true' ||
        (r.width * r.height) / Math.max(1, vw * vh) >= 0.5;
      if (ov.localName === 'dialog') {
        try { blocking = ov.matches(':modal'); } catch (e) { blocking = true; }
      }
      const head = ov.querySelector('h1,h2,h3,h4,.modal-title');
      const name = ((ov.getAttribute('aria-label') || ov.id ||
        (head && head.textContent) || ov.textContent || ''))
        .trim().replace(/\s+/g, ' ').slice(0, 60);
      return { name, blocking };
    })
    .filter(o => o.name)
    .slice(0, 3);
}
"""
)

# Interactive elements inside one child frame, for consent managers
# (Sourcepoint, Didomi, Quantcast) whose buttons live in cross-origin
# iframes the in-page snapshot walker cannot cross. Playwright's frame
# API can, so the harness scans each frame with this and allocates its
# own refs (FRAME_REF_BASE and up) resolved through a Python-side
# registry. Only light-DOM controls; ids reuse the frame's own
# __aiState so refs stay stable between scans of the same frame.
FRAME_ELEMENTS_JS = r"""
() => {
  const S = (window.__aiState = window.__aiState ||
    { ids: new WeakMap(), els: new Map(), next: 1 });
  const out = [];
  for (const el of document.querySelectorAll(
      'button, a[href], input:not([type=hidden]), select, textarea, [role=button]')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') {
      continue;
    }
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    let id = S.ids.get(el);
    if (!id) { id = S.next++; S.ids.set(el, id); }
    S.els.set(id, el);
    const name = ((el.getAttribute('aria-label') ||
      (el.innerText && el.innerText.trim()) || el.value ||
      el.getAttribute('placeholder') || el.getAttribute('title') || ''))
      .trim().replace(/\s+/g, ' ').slice(0, 70);
    const kind = el.tagName === 'A' ? 'link'
      : el.tagName === 'SELECT' ? 'select'
      : (el.tagName === 'INPUT' && !/^(submit|button|reset)$/.test(el.type))
      ? 'input' : 'button';
    out.push({ id, name, kind, disabled: el.disabled });
  }
  return out.slice(0, 30);
}
"""

# Last-resort click for styled controls whose real input sits hidden
# under a custom label: Playwright's actionability check refuses to
# click an element that does not receive pointer events, so click the
# wrapping label first, then the element itself with a raw JS click
# (HTMLElement.click() toggles checkboxes and fires input/change like
# a real click, without hit-target checks).
FALLBACK_CLICK_JS = r"""
(e) => {
  const label = (e.labels && e.labels[0]) || e.closest('label');
  if (label && label !== e) { label.click(); return 'label'; }
  e.click();
  return 'js';
}
"""

# innerText-like extraction where every <table> renders as one line per
# <tr> with cells joined by " | ", mirroring the compact view's row
# convention. Runs on a root element (or a selector); never mutates the
# DOM. Whitespace collapses like rendered text; <pre> keeps its literal
# newlines; hidden subtrees and non-content elements are skipped.
TABLE_AWARE_TEXT_JS = r"""
(root) => {
  if (typeof root === "string") root = document.querySelector(root) || document.body;
  if (!root) return "";
  const squash = (s) => (s || "").replace(/\s+/g, " ");
  const out = [];
  function rowsOf(table) {
    const lines = [];
    for (const tr of table.rows) {
      const cells = Array.from(tr.cells).map(cellText);
      if (cells.some((c) => c)) lines.push(cells.join(" | "));
    }
    return lines;
  }
  function cellText(td) {
    const nested = td.querySelector("table");
    if (nested) return rowsOf(nested).join(" / ");
    return squash(td.innerText || td.textContent || "").trim();
  }
  const SKIP = new Set(["script", "style", "noscript", "template", "svg",
                        "head", "iframe", "canvas", "object", "embed"]);
  const HIDDEN = (el) => {
    const cs = getComputedStyle(el);
    return cs.display === "none" || cs.visibility === "hidden" || el.hidden;
  };
  function walk(node, pre) {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        out.push(pre ? child.nodeValue : squash(child.nodeValue));
        continue;
      }
      if (child.nodeType !== 1 || SKIP.has(child.localName) || HIDDEN(child)) continue;
      const tag = child.localName;
      if (tag === "br") { out.push("\n"); continue; }
      if (tag === "table") { out.push("\n" + rowsOf(child).join("\n") + "\n"); continue; }
      const display = getComputedStyle(child).display;
      const block = display !== "inline" && display !== "inline-block" && display !== "contents";
      const isPre = tag === "pre";
      if (block && !isPre) out.push("\n");
      walk(child, pre || isPre);
      if (block && !isPre) out.push("\n");
    }
  }
  if (root.localName === "table") return rowsOf(root).join("\n");
  walk(root, false);
  return out.join("");
}
"""

SNAPSHOT_JS = (
    r"""(opts) => {"""
    + _OVERLAY_ROOTS_FN
    + r"""
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
  let inCell = 0;

  function flush() {
    if (inCell) return;                       // cells never flush: a row is one line
    const parts = buf;
    buf = [];
    let out = '', n = 0, clipped = false;
    for (const p of parts) {
      if (typeof p === 'object') { out += p.t; continue; }       // ref tokens are never cut or dropped
      const s = squash(p);
      if (!s) {
        if (n < MAXLINE) { out += ' '; n++; }   // whitespace between inline elements keeps words apart
        continue;
      }
      if (n >= MAXLINE) {
        if (!clipped) { out += '…'; clipped = true; }
        continue;
      }
      const room = MAXLINE - n;
      if (s.length > room) { out += s.slice(0, room) + '…'; clipped = true; n = MAXLINE; }
      else { out += p; n += s.length; }         // budget on visible length, not raw source
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
    if (isCell) inCell++;
    for (const c of kids(el)) walk(c);
    if (isCell) inCell--;
    if (lm) {
      flush();
      depth--;
      if (lines.length === mark + 1) lines.pop();   // empty landmark -> drop it
    }
    if (block || tag === 'tr') flush();
  }

  // Blocking overlays: consent banners, cookie dialogs and modals that
  // sit over the page and intercept its clicks. While one is open the
  // view collapses to just the overlay(s), so the model's next action is
  // dismissing it instead of clicking dead refs; opts.full opts out.
  const tops = __synaOverlayRoots();
  const isBlocking = (ov) => {
    if (ov.localName === 'dialog') {
      try { return ov.matches(':modal'); } catch (e) { return true; }
    }
    if (ov.classList.contains('modal')) return true;
    if (ov.getAttribute('aria-modal') === 'true') return true;
    const r = ov.getBoundingClientRect();
    return (r.width * r.height) / Math.max(1, vw * vh) >= 0.5;
  };
  const overlayName = (ov) => {
    const head = ov.querySelector('h1,h2,h3,h4,.modal-title');
    return cut(squash((ov.getAttribute('aria-label') || ov.id ||
      (head && head.textContent) || ov.textContent || '')), 60);
  };
  const area = (ov) => {
    const r = ov.getBoundingClientRect();
    return r.width * r.height;
  };
  const blocking = tops.filter(isBlocking).sort((a, b) => area(b) - area(a));
  const pageHidden = blocking.length > 0 && !opts.full;
  if (pageHidden) for (const ov of blocking) walk(ov);
  else walk(document.body || document.documentElement);
  flush();

  const de = document.documentElement;
  return { lines, scrollY: Math.round(window.scrollY), scrollH: de.scrollHeight, vh,
           pageHidden,
           blocking: blocking.map(overlayName).filter(Boolean).slice(0, 3),
           overlays: tops.map(overlayName).filter(Boolean).slice(0, 3) };
}
"""
)


class Browser:
    def __init__(self, **session_kwargs: Any):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self.last: str = ""
        self._s: BrowserSession = self.run(BrowserSession(**session_kwargs).start())

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

    def visit(self, url: str):
        return self._tool("visit", url=url)

    def snapshot(self, viewport_only: bool = False, full: bool = False):
        return self._tool("snapshot", viewport_only=viewport_only, full=full)

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

    def screenshot(self) -> bytes:
        """Capture the current viewport as PNG bytes; tools.py copies it into the pod."""
        return self.run(self._s.screenshot_bytes())

    def close(self) -> None:
        self.run(self._s.close())
        self._loop.call_soon_threadsafe(self._loop.stop)

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


# Refs at FRAME_REF_BASE and up belong to elements inside child frames
# (consent-manager iframes and the like); they resolve through the
# session's Python-side registry instead of the main world's __aiState.
FRAME_REF_BASE = 100000

# Frame URLs that only ever carry ad machinery, never consent content.
FRAME_SCAN_SKIP = ("imasdk", "doubleclick", "googlesyndication")


class ToolError(Exception):
    """Raised for errors that should be shown to the model verbatim."""


# Call-log lines that carry the actionable verdict (why the action failed),
# and the retry-cycle boilerplate that repeats until the budget is spent.
PW_REASON_PATTERN = re.compile(
    r"intercepts pointer events|intercept|not visible|hidden|detached|"
    r"obscures|not stable|does not receive pointer|resolved to|"
    r"element is not|failed to",
    re.IGNORECASE,
)
PW_NOISE_PATTERN = re.compile(
    r"^(attempting|retrying|waiting|\d+ ?×|click action|"
    r"element is (visible|enabled|editable|attached)|"
    r"scrolling into view|done scrolling|performing)",
    re.IGNORECASE,
)


def describe_pw_error(exc: Exception) -> str:
    """Condense a Playwright error into one actionable line.

    The first line carries what was attempted (and the timeout); the
    call-log lines that follow carry the actionability verdict — "element
    is not visible", "<label> intercepts pointer events", "element is
    detached" — which is exactly what the model needs to change course.
    The call log repeats its boilerplate across retry cycles, so verdict
    lines are picked by keyword first and deduplicated, with other lines
    filling the remaining budget; long DOM dumps are clipped per line.
    """
    lines = [line.strip() for line in str(exc).strip().splitlines() if line.strip()]
    if not lines:
        return "Playwright error"
    parts = [lines[0]]
    reasons, rest = [], []
    for line in lines[1:]:
        if line == "Call log:" or set(line) <= {"="}:
            continue
        clean = line.lstrip("- ").strip()
        if PW_REASON_PATTERN.search(clean):
            reasons.append(clean[:160])
        elif not PW_NOISE_PATTERN.search(clean):
            rest.append(clean[:120])
    parts.extend(list(dict.fromkeys(reasons + rest))[:5])
    return "; ".join(parts)[:600]


class BrowserSession:
    """One stealth browser context plus the page-driving tools.

    Configuration: 'headless' accepts True, False, or "virtual" (Linux);
    'humanize' enables Camoufox human-like cursor movement; 'user_data_dir'
    selects a persistent profile (cookies survive restarts); 'max_chars' is
    the hard cap on any snapshot (~3k tokens). The fingerprint OS is pinned
    to the real host (random OS = inconsistent fingerprint); override with
    os="macos"/..., or os=None to let Camoufox pick randomly.

    Request routing: every request the browser makes — agent navigation,
    page fetches, subresources, WebSockets — passes through one route
    handler (`_guard_route`): ad/identity script hosts are neutralized,
    and every other destination must pass the net_guard policy (public
    internet only). WebSocket handshakes cannot be blocked
    by routing, so they are observed and killed, and the Syna server
    rejects foreign Origins as the second layer. WebRTC is disabled:
    it bypasses HTTP-layer routing entirely and is a standard
    fingerprinting/IP-leak surface.
    """

    def __init__(
        self,
        *,
        headless: bool | str = True,
        humanize: bool | float = True,
        user_data_dir: str | None = None,
        proxy: dict | None = None,
        geoip: bool = False,
        max_chars: int = 12000,
        **camoufox_kwargs: Any,
    ):
        self.max_chars = max_chars
        camoufox_kwargs.setdefault("os", detect_host_os())
        prefs = camoufox_kwargs.setdefault("firefox_user_prefs", {})
        prefs.setdefault("media.peerconnection.enabled", False)
        self._kw = dict(
            headless=headless, humanize=humanize, geoip=geoip, **camoufox_kwargs
        )
        if proxy:
            self._kw["proxy"] = proxy
        self._persistent = user_data_dir is not None
        if self._persistent:
            self._kw.update(
                persistent_context=True,
                user_data_dir=user_data_dir,
                accept_downloads=False,
            )
        self._cm = None
        self.context = None
        self.page = None
        self._background_tasks: set[asyncio.Task] = set()
        self._last: tuple[str, list[str]] | None = None
        self._last_nav_url: str | None = None
        self._last_click_at = 0.0
        self._frame_refs: dict[int, tuple[Any, int]] = {}
        self._frame_next = FRAME_REF_BASE

    async def start(self) -> "BrowserSession":
        self._cm = AsyncCamoufox(**self._kw)
        obj = await self._cm.__aenter__()
        if self._persistent:
            self.context = obj
        else:
            self.context = await obj.new_context(accept_downloads=False)
        await self.context.route("**/*", self._guard_route)
        self.page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )
        self._watch_page(self.page)
        self.context.on("page", self._on_new_page)
        return self

    @staticmethod
    async def _fulfill_empty_script(route) -> None:
        """Answer a blocked ad/identity script with harmless empty JavaScript."""
        try:
            await route.fulfill(
                status=200,
                content_type="application/javascript",
                body="/* neutralized by syna: script host blocked */",
            )
        except Exception:
            pass

    async def _guard_route(self, route) -> None:
        """Single choke point for every request the browser makes.

        One handler for all URLs: ad/identity script hosts get empty
        JavaScript, and every other destination must pass the net_guard
        policy (public internet only) or the request is aborted before
        it leaves the machine. Unexpected errors fail closed — the
        request is aborted and the failure logged, never silently passed.
        """
        url = route.request.url
        try:
            if self._is_neutralized_url(url):
                await self._fulfill_empty_script(route)
                return
            decision = net_guard.check_url(url)
            if decision.allowed:
                await route.continue_()
                return
            get_logger().info(
                "Browser blocked request to %.200s: %s", url, decision.reason
            )
            await route.abort("blocked")
        except Exception:
            get_logger().exception(
                "Guard route handling failed for %.200s; failing closed", url
            )
            try:
                await route.abort("blocked")
            except Exception:
                pass

    @staticmethod
    def _is_neutralized_url(url: str) -> bool:
        """Whether a URL targets one of the neutralized ad/identity hosts.

        Matched on the parsed hostname and optional path prefix — never
        on the raw URL text, so a query-string lookalike cannot make an
        innocent page match.
        """
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        for host_pattern, path_prefix in NEUTRALIZED_SCRIPT_HOSTS:
            if host_pattern.startswith("*."):
                if not host.endswith(host_pattern[1:]):
                    continue
            elif host != host_pattern:
                continue
            if parsed.path.startswith(path_prefix):
                return True
        return False

    def _spawn(self, coro) -> None:
        """Schedule a fire-and-forget task while keeping a reference to it.

        asyncio only holds weak references to running tasks: without keeping
        one, a task can be garbage-collected mid-flight and any exception it
        raises is silently dropped.
        """
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _on_websocket(self, ws) -> None:
        """Kill WebSocket connections that bypass the request policy.

        Routing observes but cannot block a WS handshake: the socket is
        closed as soon as it appears. The Syna server's Origin check is
        the second layer, and pages cannot read loopback responses
        without same-origin regardless.
        """
        decision = net_guard.check_url(ws.url)
        if not decision.allowed:
            get_logger().info(
                "Browser blocked WebSocket to %.200s: %s", ws.url, decision.reason
            )
            self._spawn(self._close_websocket(ws))

    async def _close_websocket(self, ws) -> None:
        try:
            await ws.close()
        except Exception:
            pass

    async def _on_download(self, download) -> None:
        """Belt-and-braces guard: kill any download attempt in flight.

        accept_downloads=False already blocks the save; cancelling here also
        stops the browser from holding partial data in memory.
        """
        try:
            await download.cancel()
        except Exception:
            pass

    def _watch_page(self, p) -> None:
        p.on("download", self._on_download)
        p.on("crash", self._on_crash)
        p.on("pageerror", self._on_page_error)
        p.on("requestfailed", self._on_request_failed)
        p.on("websocket", self._on_websocket)

    def _on_crash(self, page) -> None:
        """A renderer crash must never end the session: reload transparently."""
        get_logger().warning("Browser page crashed: %.200s", page.url)
        self._last = None
        self._spawn(self._recover_from_crash(page))

    def _on_page_error(self, error) -> None:
        get_logger().debug("Page JS error: %.300s", error)

    def _on_request_failed(self, request) -> None:
        failure = str(request.failure) if request.failure else ""
        if "ABORTED" in failure.upper():
            return
        get_logger().debug(
            "Request failed: %s %.200s (%s)", request.method, request.url, failure
        )

    async def _recover_from_crash(self, page) -> None:
        """Reload the crashed page (or open a fresh tab) so tools keep working."""
        try:
            await asyncio.sleep(0.5)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=20000)
            except PWError:
                page = await self.context.new_page()
                self._watch_page(page)
                self.page = page
                if self._last_nav_url:
                    await page.goto(
                        self._last_nav_url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
            await self._settle()
            get_logger().info("Browser page recovered after crash")
        except Exception as exc:
            get_logger().warning("Browser crash recovery failed: %s", exc)

    async def close(self) -> None:
        if self._cm:
            await self._cm.__aexit__(None, None, None)
            self._cm = None

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.close()

    def _on_new_page(self, p) -> None:
        """Follow a tab opened by a click; close unsolicited popups.

        Fandom-style malvertising loves `window.open` spam: adopting those
        popups as the current page silently hijacks the session.
        """
        self._watch_page(p)
        if time.monotonic() - self._last_click_at > POPUP_ADOPT_WINDOW_S:
            get_logger().info("Closing unsolicited popup: %.200s", p.url)
            self._spawn(self._close_quietly(p))
            return
        self.page = p
        self._last = None

    async def _close_quietly(self, p) -> None:
        try:
            await p.close()
        except Exception:
            pass

    def _live_page(self):
        if self.page.is_closed():
            pages = [p for p in self.context.pages if not p.is_closed()]
            if not pages:
                raise ToolError("all tabs are closed")
            self.page = pages[-1]
        return self.page

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
            return "ERROR: " + describe_pw_error(e)

    async def _raw_snapshot(self, viewport_only: bool, full: bool = False) -> dict:
        """Run the snapshot walker, retrying once when a navigation destroys the context."""
        page = self._live_page()
        for attempt in (0, 1):
            try:
                return await page.evaluate(
                    SNAPSHOT_JS,
                    {"viewportOnly": viewport_only, "maxLine": 240, "full": full},
                )
            except PWError as e:
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

    async def _scan_frames(self) -> list[str]:
        """List interactive elements inside child frames, allocating refs.

        Consent managers (Sourcepoint, Didomi, Quantcast) render their
        buttons inside cross-origin iframes the in-page walker cannot
        cross; Playwright's frame API can. Only called for views where an
        overlay blocks the page, so ordinary iframe ads add no noise.
        """
        lines: list[str] = []
        page = self._live_page()
        for frame in page.frames:
            if frame is page.main_frame or frame.url == "about:blank":
                continue
            if any(skip in frame.url for skip in FRAME_SCAN_SKIP):
                continue
            try:
                entries = await asyncio.wait_for(
                    frame.evaluate(FRAME_ELEMENTS_JS), timeout=4
                )
            except Exception:
                continue
            if not entries:
                continue
            host = urlparse(frame.url).netloc or "frame"
            lines.append(f"(frame {host} — its controls get refs below)")
            for entry in entries:
                gref = self._frame_next
                self._frame_next += 1
                self._frame_refs[gref] = (frame, entry["id"])
                state = " disabled" if entry.get("disabled") else ""
                name = f' "{entry["name"]}"' if entry["name"] else ""
                lines.append(f"[{gref}] {entry['kind']}{name}{state}")
        return lines

    async def _observe(
        self, *, viewport_only: bool = False, diff: bool = False, full: bool = False
    ) -> str:
        data = await self._raw_snapshot(viewport_only, full)
        page = self._live_page()
        url, lines = page.url, data["lines"]
        blocking = data.get("blocking") or []
        if data.get("pageHidden") or blocking:
            lines = lines + await self._scan_frames()
        head = (
            f"URL: {url}\nTitle: {await page.title()}\n"
            f"Scroll: {data['scrollY']}/{data['scrollH']}px (viewport {data['vh']})\n"
        )
        blocking = data.get("blocking") or []
        if data.get("pageHidden"):
            head += (
                "(note: a blocking overlay is open: "
                + " | ".join(blocking)
                + " — the rest of the page is hidden until it is dismissed;"
                " interact with the overlay above, or call snapshot with"
                " full=true to see the page anyway)\n"
            )
        elif blocking:
            head += (
                "(note: a blocking overlay is open: "
                + " | ".join(blocking)
                + " — clicks underneath will not land until it is dismissed"
                " (page shown anyway: full=true))\n"
            )
        elif data.get("overlays"):
            head += (
                "(note: overlay open on the page: "
                + " | ".join(data["overlays"])
                + " — dismiss it if it is in the way)\n"
            )
        prev, self._last = self._last, (url, lines)

        if diff and prev and prev[0] == url:
            old = set(prev[1])
            new = [line for line in lines if line not in old]
            removed = len(old - set(lines))
            if len(new) < 0.6 * max(len(lines), 1):
                if not new and not removed:
                    return head + "(no visible changes)"
                body = "\n".join("+ " + line.strip() for line in new)
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

    async def _wait_past_challenge(self, max_wait: float = 15.0) -> None:
        """Wait out an anti-bot interstitial until real page text appears.

        Returns as soon as no challenge marker is on the page, so ordinary
        visits pay no wait. If the interstitial outlasts the budget, the
        view is returned anyway: the model sees the challenge text and
        decides (retry, give up, or report the block) instead of getting a
        bare error. Never treats marker-bearing normal content as a block.
        """
        page = self._live_page()
        deadline = time.monotonic() + max_wait
        while True:
            try:
                body = (await page.inner_text("body")).strip().lower()
            except PWError:
                body = ""
            if not body or not any(marker in body for marker in CHALLENGE_MARKERS):
                return
            if time.monotonic() >= deadline:
                get_logger().warning(
                    "Page still shows an anti-bot challenge after %.0fs: %.200s",
                    max_wait,
                    page.url,
                )
                return
            await asyncio.sleep(1.0)

    async def _dismiss_blocking_overlays(self) -> int:
        """Dismiss visible blocking overlays (banners, dialogs, modals).

        Best-effort and silent on failure; returns how many overlays a
        dismissal control was clicked in. Only consent-language buttons
        and pure-dismiss controls are clicked — never Save/OK/submit.
        """
        try:
            dismissed = await self._live_page().evaluate(OVERLAY_DISMISS_JS)
        except Exception:
            get_logger().debug("Overlay dismissal failed", exc_info=True)
            return 0
        if dismissed:
            get_logger().debug("Dismissed %d blocking overlay(s).", dismissed)
            await asyncio.sleep(random.uniform(0.4, 0.9))
        return dismissed

    async def _human_scroll(self, max_steps: int = 10) -> None:
        """Scroll gradually with random pauses to trigger lazy-loading.

        Shorter than the fetcher's full-page sweep: the compact view only
        shows the viewport, and the model can scroll further herself.
        """
        page = self._live_page()
        try:
            pos, steps = 0, 0
            while steps < max_steps:
                pos += random.randint(300, 700)
                await page.evaluate(
                    f"window.scrollTo({{top: {pos}, behavior: 'smooth'}})"
                )
                await asyncio.sleep(random.uniform(0.2, 0.5))
                steps += 1
                height = await page.evaluate("document.documentElement.scrollHeight")
                if pos >= height:
                    break
            await asyncio.sleep(random.uniform(0.3, 0.6))
            await page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
            await asyncio.sleep(random.uniform(0.3, 0.5))
        except Exception:
            get_logger().debug("Lazy-load scroll interrupted", exc_info=True)

    async def _wait_dom_stable(self, max_wait: float = 4.0) -> None:
        """Wait until the visible text stops changing (dynamic content)."""
        page = self._live_page()
        deadline = time.monotonic() + max_wait
        last, stable = -1, 0
        while time.monotonic() < deadline and stable < 2:
            try:
                size = len((await page.inner_text("body")).strip())
            except PWError:
                size = -1
            stable = stable + 1 if size == last else 0
            last = size
            await asyncio.sleep(0.4)

    async def _el(self, ref: int):
        page = self._live_page()
        if ref >= FRAME_REF_BASE:
            entry = self._frame_refs.get(ref)
            if entry is None:
                raise ToolError(
                    f"ref {ref} not found (page changed?). Call snapshot to refresh refs."
                )
            frame, local_id = entry
            h = await frame.evaluate_handle(
                "(id) => (window.__aiState && window.__aiState.els.get(id)) || null",
                local_id,
            )
            el = h.as_element()
            if el is None:
                raise ToolError(
                    f"ref {ref} not found (page changed?). Call snapshot to refresh refs."
                )
            return el
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

    async def tool_visit(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            raise ToolError(
                f"blocked protocol '{parsed.scheme or 'none'}:' — only http and"
                " https URLs can be opened"
            )
        decision = net_guard.check_url(url, schemes=net_guard.HTTP_SCHEMES)
        if not decision.allowed:
            raise ToolError(
                f"blocked navigation to {url}: {decision.reason}. Syna's browser"
                " can only open public internet addresses; to work with a local"
                " or private service, use execute_command inside the pod instead."
            )
        page = self._live_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._last_nav_url = url
        self._frame_refs.clear()
        await self._settle()
        await self._wait_past_challenge()
        await self._dismiss_blocking_overlays()
        await self._human_scroll()
        await self._wait_dom_stable()
        self._last = None
        return await self._observe()

    async def tool_snapshot(
        self, viewport_only: bool = False, full: bool = False
    ) -> str:
        return await self._observe(viewport_only=viewport_only, full=full)

    async def _click_element(self, el) -> str | None:
        """Click via Playwright, falling back to overlay dismissal, a
        trusted coordinate click, and label/JS clicks.

        Returns a short note when a fallback path was used, or None for a
        normal click. Raises ToolError carrying the actionability reason
        when every path fails. Overlays are dismissed only when the
        clicked element sits outside them: if the model is working inside
        a consent banner, auto-accepting it would override the granular
        interaction it was attempting.
        """
        try:
            await el.click(timeout=7000)
            return None
        except PWError as exc:
            reason = describe_pw_error(exc)
        try:
            inside_overlay = await el.evaluate(
                "(e) => {"
                + _OVERLAY_ROOTS_FN
                + " return __synaOverlayRoots().some(o => o.contains(e)); }"
            )
        except PWError:
            inside_overlay = False
        dismissed = 0 if inside_overlay else await self._dismiss_blocking_overlays()
        if dismissed:
            try:
                await el.click(timeout=4000)
                return (
                    f"click worked after {dismissed} blocking"
                    f" overlay(s) were dismissed ({reason})"
                )
            except PWError as exc2:
                reason = describe_pw_error(exc2)
        page = self._live_page()
        try:
            await el.evaluate(
                "e => e.scrollIntoView({block: 'center', behavior: 'instant'})"
            )
            await asyncio.sleep(0.3)
            box = await el.bounding_box()
            if box:
                await page.mouse.click(
                    box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                )
                return f"coordinate click used ({reason})"
        except PWError:
            pass
        try:
            path = await el.evaluate(FALLBACK_CLICK_JS)
            return f"{path} click used ({reason})"
        except PWError:
            raise ToolError(reason) from exc

    async def tool_click(self, ref: int) -> str:
        el = await self._el(ref)
        await el.scroll_into_view_if_needed(timeout=5000)
        self._last_click_at = time.monotonic()
        note = await self._click_element(el)
        await self._settle()
        view = await self._observe(diff=True)
        return (f"(note: {note})\n{view}") if note else view

    async def tool_type(
        self, ref: int, text: str, clear: bool = True, submit: bool = False
    ) -> str:
        """Type text into an input/textarea, clearing it first by default.

        click() above focuses the element; keys are typed with jittered
        per-key timing. submit=True presses Enter afterwards.
        """
        el = await self._el(ref)
        await el.scroll_into_view_if_needed(timeout=5000)
        self._last_click_at = time.monotonic()
        note = await self._click_element(el)
        if clear:
            await el.fill("")
        kb = self._live_page().keyboard
        for ch in text:
            await kb.type(ch)
            await asyncio.sleep(random.uniform(0.03, 0.12))
        if submit:
            await self._live_page().keyboard.press("Enter")
            await self._settle()
        view = await self._observe(diff=True)
        return (f"(note: {note})\n{view}") if note else view

    async def tool_select(self, ref: int, option: str) -> str:
        el = await self._el(ref)
        self._last_click_at = time.monotonic()
        try:
            await el.select_option(label=option, timeout=3000)
        except PWError:
            await el.select_option(value=option, timeout=3000)
        await self._settle()
        return await self._observe(diff=True)

    async def tool_press(self, key: str) -> str:
        self._last_click_at = time.monotonic()
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
        self._last_nav_url = self._live_page().url
        self._frame_refs.clear()
        await self._settle()
        self._last = None
        return await self._observe()

    async def tool_read(self, ref: int | None = None, max_chars: int = 6000) -> str:
        """Full untruncated text of one element (or the whole page) for extraction.

        Form controls report their current value instead of text: inputs and
        textareas return their value, selects return the chosen option plus
        the full option list. Password fields stay masked. Tables render one
        line per row with cells separated by " | ", so rows survive as data.
        """
        if ref is None:
            page = self._live_page()
            text = await page.evaluate(TABLE_AWARE_TEXT_JS, "body")
            if not text.strip():
                await asyncio.sleep(0.4)
                text = await page.evaluate(TABLE_AWARE_TEXT_JS, "body")
            if not text.strip():
                return (
                    "(no text returned — the document is blank. It may have been"
                    " replaced by an overlay or crashed; call visit to reload it.)"
                )
            try:
                scan = await page.evaluate(BLOCKING_OVERLAYS_JS)
            except PWError:
                scan = []
            blocked = [o["name"] for o in scan if o.get("blocking")]
            if blocked:
                text = (
                    "(note: a blocking overlay is open: "
                    + " | ".join(blocked)
                    + " — this text includes content that clicks cannot reach"
                    " until the overlay is dismissed)\n" + text
                )
        else:
            el = await self._el(ref)
            info = await el.evaluate(
                "e => ({tag: e.localName, type: e.type || '',"
                " editable: e.isContentEditable})"
            )
            tag = info.get("tag", "")
            if tag == "select":
                text = await el.evaluate(
                    "e => 'selected: '"
                    " + Array.from(e.selectedOptions).map(o => o.text.trim()).join(' | ')"
                    " + '\\nall options: '"
                    " + Array.from(e.options).map(o => o.text.trim()).join(' | ')"
                )
            elif tag in ("input", "textarea"):
                if info.get("type") == "password":
                    text = "(password value hidden)"
                else:
                    text = await el.input_value()
            else:
                text = await el.evaluate(TABLE_AWARE_TEXT_JS)
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        return text[:max_chars] + ("\n... [truncated]" if len(text) > max_chars else "")

    async def screenshot_bytes(self) -> bytes:
        """PNG bytes of the current viewport for describe_image inspection."""
        return await self._live_page().screenshot(type="png")


if __name__ == "__main__":

    async def demo():
        async with BrowserSession(headless=False) as s:
            print(await s.call("visit", {"url": "https://news.ycombinator.com"}))
            print(await s.call("click", {"ref": 5}))

    asyncio.run(demo())
