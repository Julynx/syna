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
  return md.render(String(source));
}

function applyLinkPolicy(container) {
  container.querySelectorAll("a[href]").forEach((link) => {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  });
}
