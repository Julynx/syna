"use strict";

const md =
  typeof window.markdownit === "function"
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

const MATH_RE = /\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)/g;
const INLINE_DOLLAR_RE = /\$([^\s$](?:[^$\n]*?[^\s$])?)\$(?!\d)/g;
const MATH_TOKEN_RE = /MQ(\d+)QM/g;

const MATH_DELIMITERS = [
  { left: "$$", right: "$$", display: true },
  { left: "\\[", right: "\\]", display: true },
  { left: "\\(", right: "\\)", display: false },
];

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function extractMath(source) {
  const codeStore = [];
  const dollarStore = [];
  const mathStore = [];
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
    return "MQ" + (mathStore.length - 1) + "QM";
  });
  text = text.replace(INLINE_DOLLAR_RE, (m, inner) => {
    if (/^\d+(?:[.,]\d+)?$/.test(inner)) return m;
    mathStore.push({ src: "$" + inner + "$", inline: true });
    return "MQ" + (mathStore.length - 1) + "QM";
  });
  const unshieldDollars = (s) =>
    // biome-ignore lint/suspicious/noControlCharactersInRegex: \x00D is a placeholder sentinel for shielded `$` text
    s.replace(/\x00D(\d+)\x00/g, (_, i) => dollarStore[+i]);
  text = unshieldDollars(text);
  // biome-ignore lint/suspicious/noControlCharactersInRegex: \x00C is a placeholder sentinel for shielded code blocks
  text = text.replace(/\x00C(\d+)\x00/g, (_, i) => codeStore[+i]);
  mathStore.forEach((item) => {
    item.src = unshieldDollars(item.src);
  });
  return { text, mathStore };
}

function renderInlineMath(src) {
  if (typeof window.katex === "undefined" || !window.katex.renderToString)
    return null;
  try {
    return window.katex.renderToString(src, {
      displayMode: false,
      throwOnError: false,
    });
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
      throwOnError: false,
    });
  } catch (err) {
    console.error(err);
  }
}

function applyLinkPolicy(container) {
  container.querySelectorAll("a[href]").forEach((link) => {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  });
}
