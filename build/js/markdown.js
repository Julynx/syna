/* ==== js/markdown.js ==== */
"use strict";

// Markdown rendering for model messages. Delegated to the vendored markdown-it
// library (MIT, see assets/vendor/markdown-it.LICENSE.txt). It runs with
// html: false, so raw HTML in model output is escaped, keeping the renderer
// XSS-safe. When the vendor file is missing, a minimal escaped-text fallback
// keeps messages readable.

const md = typeof window.markdownit === "function"
  ? window.markdownit({ html: false, breaks: true, linkify: false })
  : null;

function renderMarkdown(source) {
  if (!md) {
    const text = String(source)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    return "<p>" + text.replace(/\n/g, "<br>") + "</p>";
  }
  const extracted = extractMath(source);
  return restoreMath(md.render(extracted.text), extracted.mathStore);
}

// ---------- LaTeX math inside model messages ----------
// Rendered by the vendored KaTeX after markdown. Two extra passes are needed
// because markdown-it would otherwise eat the backslashes ("\(" comes out of
// the parser as "("): math spans are lifted out before rendering, restored
// verbatim (HTML-escaped) after, and KaTeX auto-render then finds them in the
// DOM. Fenced and inline code are shielded first, so math inside code blocks
// stays literal.
//
// Display math ($$..$$, \[..\]) and \(..\) go through auto-render, whose
// delimiter list never gets a bare '$': inside auto-render any two literal
// dollar signs in the DOM would pair up and turn prices into math. Inline
// $..$ is instead extracted here — with pandoc-style guards — and rendered
// directly via katex.renderToString at restore time. The guards: the opening
// '$' is unescaped ("\$" is shielded first) and not followed by whitespace,
// the content sits on one line with non-space edges, and the closing '$' is
// not followed by a digit. So "$x$" renders, while "$5 and $10" or "$2+$3"
// stay literal prices.
const MATH_RE = /\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)/g;
// Inline $..$: one line, non-space edges, no digit right after the closing '$'.
const INLINE_DOLLAR_RE = /\$([^\s$](?:[^$\n]*?[^\s$])?)\$(?!\d)/g;
const MATH_TOKEN_RE = /MQ(\d+)QM/g;

const MATH_DELIMITERS = [
  { left: "$$", right: "$$", display: true },
  { left: "\\[", right: "\\]", display: true },
  { left: "\\(", right: "\\)", display: false }
];

function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function extractMath(source) {
  const codeStore = [];
  const dollarStore = [];
  const mathStore = []; // items: { src, inline }
  // shield fenced + inline code so neither math extraction nor the placeholder
  // rewrite touches code content, then shield escaped dollars so a literal
  // "\$" can never open a $..$ span (restored before markdown runs, so "\$"
  // still renders as a plain '$' outside math)
  let text = String(source)
    .replace(/```[\s\S]*?```|~~~[\s\S]*?~~~/g, (m) => {
      codeStore.push(m);
      return "\x00C" + (codeStore.length - 1) + "\x00";
    })
    .replace(/`[^`\n]*`/g, (m) => {
      codeStore.push(m);
      return "\x00C" + (codeStore.length - 1) + "\x00";
    })
    .replace(/\\\$/g, (m) => {
      dollarStore.push(m);
      return "\x00D" + (dollarStore.length - 1) + "\x00";
    });
  text = text.replace(MATH_RE, (m) => {
    mathStore.push({ src: m, inline: false });
    return "MQ" + (mathStore.length - 1) + "QM"; // plain-word token survives markdown
  });
  text = text.replace(INLINE_DOLLAR_RE, (m, inner) => {
    if (/^\d+(?:[.,]\d+)?$/.test(inner)) return m; // "$19.99$" is currency, not math
    mathStore.push({ src: "$" + inner + "$", inline: true });
    return "MQ" + (mathStore.length - 1) + "QM";
  });
  // un-shield: back to "\$" in both the markdown text and any math span that
  // legitimately contained an escaped dollar (KaTeX renders "\$" as '$')
  const unshieldDollars = (s) => s.replace(/\x00D(\d+)\x00/g, (_, i) => dollarStore[+i]);
  text = unshieldDollars(text);
  text = text.replace(/\x00C(\d+)\x00/g, (_, i) => codeStore[+i]);
  mathStore.forEach((item) => { item.src = unshieldDollars(item.src); });
  return { text, mathStore };
}

// Inline spans are rendered directly instead of going through auto-render:
// katex.renderToString returns finished HTML, so no bare '$' delimiter ever
// has to exist for auto-render. Falls back to the escaped literal when the
// vendored KaTeX is unavailable.
function renderInlineMath(src) {
  if (typeof window.katex === "undefined" || !window.katex.renderToString) return null;
  try {
    return window.katex.renderToString(src, { displayMode: false, throwOnError: false });
  } catch (err) {
    return null;
  }
}

function restoreMath(html, mathStore) {
  return html.replace(MATH_TOKEN_RE, (_, i) => {
    const item = mathStore[+i];
    if (!item) return "";
    if (item.inline) {
      const rendered = renderInlineMath(item.src.slice(1, -1));
      if (rendered !== null) return rendered;
    }
    return escapeHtml(item.src);
  });
}

function renderMath(container) {
  if (typeof window.renderMathInElement !== "function") return;
  try {
    window.renderMathInElement(container, {
      delimiters: MATH_DELIMITERS,
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
      throwOnError: false
    });
  } catch (err) { console.error(err); }
}

function applyLinkPolicy(container) {
  container.querySelectorAll("a[href]").forEach((link) => {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  });
}

