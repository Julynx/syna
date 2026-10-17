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
// stays literal. Only the safe delimiters are enabled ($$..$$, \[..\], \(..\))
// — single '$' stays off so prices like "$19.99" can never turn into math.
const MATH_RE = /\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)/g;
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
  const mathStore = [];
  // shield fenced + inline code so neither math extraction nor the placeholder
  // rewrite touches code content
  let text = String(source)
    .replace(/```[\s\S]*?```|~~~[\s\S]*?~~~/g, (m) => {
      codeStore.push(m);
      return "\x00C" + (codeStore.length - 1) + "\x00";
    })
    .replace(/`[^`\n]*`/g, (m) => {
      codeStore.push(m);
      return "\x00C" + (codeStore.length - 1) + "\x00";
    });
  text = text.replace(MATH_RE, (m) => {
    mathStore.push(m);
    return "MQ" + (mathStore.length - 1) + "QM"; // plain-word token survives markdown
  });
  text = text.replace(/\x00C(\d+)\x00/g, (_, i) => codeStore[+i]);
  return { text, mathStore };
}

function restoreMath(html, mathStore) {
  return html.replace(MATH_TOKEN_RE, (_, i) => escapeHtml(mathStore[+i] || ""));
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

