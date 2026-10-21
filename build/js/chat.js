"use strict";

const chat = document.getElementById("chat");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const attachBtn = document.getElementById("attach-btn");
const newchatBtn = document.getElementById("newchat-btn");
const fileInput = document.getElementById("file-input");
const statusRow = document.createElement("div");
statusRow.className = "inline-status glass";
statusRow.innerHTML =
  '<span class="status-dot"></span><span class="status-label"></span>';
const statusDot = statusRow.querySelector(".status-dot");
const statusLabel = statusRow.querySelector(".status-label");
const banner = document.getElementById("banner");

const MAX_VISIBLE_TOOL_CALLS =
  (window.SYNA_CONFIG && window.SYNA_CONFIG.max_visible_tool_calls) || 3;
let toolRun = null;

const CLIENT_ID_KEY = "syna-client-id";
let clientId = localStorage.getItem(CLIENT_ID_KEY);
if (!clientId) {
  clientId = crypto.randomUUID();
  localStorage.setItem(CLIENT_ID_KEY, clientId);
}

const isServed =
  location.protocol === "http:" || location.protocol === "https:";
const API_BASE = isServed ? location.origin : "http://127.0.0.1:8000";
const WS_BASE = isServed
  ? (location.protocol === "https:" ? "wss://" : "ws://") + location.host
  : "ws://127.0.0.1:8000";

let ws = null;
let reconnectDelay = 1000;
let busy = false;
let replaying = true;

/* Performance state: cached layout constants and tracked elements so
   per-event work stays O(1) instead of scanning a long chat. The replay
   queue batches history rendering into animation-frame slices so a long
   reconnect cannot freeze the page. */
const CHAT_ROW_GAP = parseFloat(getComputedStyle(chat).rowGap) || 10;
const REPLAY_FRAME_BUDGET_MS = 8;
let runningToolRow = null;
let openFileGroup = null;
let thinkingRow = null;
let replayQueue = [];
let replayFlushPending = false;

function scheduleReplayFlush() {
  if (replayFlushPending) return;
  replayFlushPending = true;
  requestAnimationFrame(flushReplay);
}

function flushReplay() {
  replayFlushPending = false;
  if (!replayQueue.length) return;
  const stick = isAtBottom();
  const start = performance.now();
  while (
    replayQueue.length &&
    performance.now() - start < REPLAY_FRAME_BUDGET_MS
  ) {
    render(replayQueue.shift());
  }
  if (stick) scrollToBottom();
  if (replayQueue.length) scheduleReplayFlush();
  else if (!replaying) followBottom(700);
}

const REDUCE = matchMedia("(prefers-reduced-motion: reduce)").matches;
const EASE_OUT = "cubic-bezier(0.22, 1, 0.36, 1)";
const EASE_IN = "cubic-bezier(0.65, 0, 0.35, 1)";

const GLYPH = (name, cls) =>
  '<span class="material-symbols-outlined' +
  (cls ? " " + cls : "") +
  '">' +
  name +
  "</span>";

const ICONS = {
  send: GLYPH("send"),
  stop: GLYPH("stop"),
  clip: GLYPH("attach_file"),
  file: GLYPH("insert_drive_file"),
  terminal: GLYPH("terminal"),
  globe: GLYPH("public"),
  newchat: GLYPH("refresh"),
  search: GLYPH("search"),
  pencil: GLYPH("edit"),
  eye: GLYPH("visibility"),
  share: GLYPH("upload"),
  warn: GLYPH("warning"),
  copy: GLYPH("content_copy", "i-copy"),
  check: GLYPH("check", "i-check"),
  history: GLYPH("history"),
  mic: GLYPH("mic"),
  think: GLYPH("psychology"),
  image: GLYPH("image"),
  speakerOn: GLYPH("record_voice_over", "s-on"),
  speakerOff: GLYPH("voice_over_off", "s-off"),
};

const TOOL_META = {
  think: { verb: "Noted", icon: ICONS.think, key: "thought" },
  execute_command: {
    verb: "Ran command",
    icon: ICONS.terminal,
    key: "command",
  },
  web_interact: { verb: "Browsed", icon: ICONS.globe },
  web_search: { verb: "Searched", icon: ICONS.search, key: "query" },
  read_file: { verb: "Read file", icon: ICONS.eye, key: "file_path" },
  edit_file: { verb: "Edited file", icon: ICONS.pencil, key: "file_path" },
  describe_image: {
    verb: "Described image",
    icon: ICONS.image,
    key: "file_path",
  },
  speech_to_text: {
    verb: "Transcribed",
    icon: ICONS.mic,
    key: "file_path",
  },
  text_to_speech: {
    verb: "Synthesized",
    icon: ICONS.speakerOn,
    key: "file_path",
  },
  text_to_image: {
    verb: "Generated image",
    icon: ICONS.image,
    key: "prompt",
  },
  share_file: { verb: "Share file", icon: ICONS.share, key: "path" },
};

const DETAIL_CAP_CHARS = 6000;

function formatSize(bytes) {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + " KB";
  return bytes + " B";
}

function firstQuotedArg(signature) {
  const text = String(signature);
  // Python repr switches to double quotes when a value contains single
  // quotes (e.g. heredoc commands), so match whichever quote character
  // directly follows the '=' of the first keyword argument.
  const eq = text.indexOf("=");
  if (eq >= 0) {
    const m = text
      .slice(eq + 1)
      .match(/^ *('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")/);
    if (m) return m[1].slice(1, -1).replace(/\\(['"\\])/g, "$1");
  }
  const quoted = text.match(/'((?:[^'\\]|\\.)*)'/);
  if (quoted) return quoted[1].replace(/\\'/g, "'").replace(/\\\\/g, "\\");
  const bare = text.match(/=\s*([^,)]+)/);
  return bare ? bare[1].trim() : "";
}

function primaryArg(event) {
  const meta = TOOL_META[event.name];
  if (event.arguments && meta && meta.key) {
    const value = event.arguments[meta.key];
    if (value !== undefined && value !== null) return String(value);
  }
  return firstQuotedArg(event.signature);
}

function truncateArg(value) {
  if (!value) return "";
  return value.length > 32 ? value.slice(0, 32) + "..." : value;
}

function isAtBottom() {
  return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80;
}

function scrollToBottom() {
  chat.scrollTop = chat.scrollHeight;
}

function append(element) {
  const stick = replaying ? false : isAtBottom();
  if (statusRow.parentNode === chat) chat.insertBefore(element, statusRow);
  else chat.appendChild(element);
  if (replaying) return;
  growIn(element);
  if (stick) followBottom(700);
}

function cancelAnim(host) {
  if (host._anim) {
    host._anim.onfinish = null;
    host._anim.cancel();
    host._anim = null;
  }
}

function addBubble(content, side, queued) {
  const bubble = document.createElement("div");
  bubble.className = "bubble " + side;
  const rendered = side === "model" ? renderMarkdown(content) : "";
  if (rendered) {
    bubble.classList.add("markdown");
    bubble.innerHTML = rendered;
    applyLinkPolicy(bubble);
    renderMath(bubble);
  } else {
    bubble.textContent = content;
  }
  if (side === "model") addCopyButton(bubble, content);
  append(bubble);
  if (queued) {
    const tag = document.createElement("div");
    tag.className = "queued-tag";
    tag.textContent = "queued";
    append(tag);
  }
}

const COPY_FEEDBACK_MS = 1400;

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext)
    return navigator.clipboard.writeText(text);
  return new Promise((resolve, reject) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy")
        ? resolve()
        : reject(new Error("copy rejected"));
    } catch (err) {
      reject(err);
    } finally {
      ta.remove();
    }
  });
}

function addCopyButton(bubble, source) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-btn glass";
  btn.title = "Copy markdown";
  btn.setAttribute("aria-label", "Copy message as markdown");
  btn.innerHTML = ICONS.copy + ICONS.check;
  btn.addEventListener("click", () => {
    copyText(source)
      .then(() => {
        btn.classList.add("copied");
        clearTimeout(btn._t);
        btn._t = setTimeout(
          () => btn.classList.remove("copied"),
          COPY_FEEDBACK_MS,
        );
      })
      .catch(() => {});
  });
  bubble.appendChild(btn);
}

function toolEventText(event) {
  if (event.parse_error) return "invalid tool call format";
  const meta = TOOL_META[event.name];
  if (!meta) return truncateArg(event.signature);
  if (event.name === "web_interact") return webInteractText(event);
  const arg = truncateArg(primaryArg(event));
  let verb = meta.verb;
  if (event.name === "web_search" && /^https?:\/\//.test(primaryArg(event)))
    verb = "Visited";
  return verb + (arg ? ' "' + arg + '"' : "");
}

const WEB_VERBS = {
  goto: "Visited",
  snapshot: "Read page",
  click: "Clicked",
  type: "Typed",
  select: "Selected",
  press: "Pressed",
  scroll: "Scrolled",
  back: "Went back",
  read: "Read",
  screenshot: "Captured",
};

function webInteractText(event) {
  const args = event.arguments || null;
  const signature = String(event.signature);
  const cmd = args ? String(args.command || "") : firstQuotedArg(signature);
  const arg = (key) => {
    if (args && args[key] !== undefined && args[key] !== null)
      return String(args[key]);
    const match = signature.match(new RegExp("'" + key + "':\\s*'([^']*)'"));
    return match ? match[1] : null;
  };
  if (cmd === "goto") return 'Visited "' + truncateArg(arg("url") || "") + '"';
  if (cmd === "click" || cmd === "read") {
    const ref = args ? args.ref : (signature.match(/'ref':\s*(\d+)/) || [])[1];
    return WEB_VERBS[cmd] + (ref ? " #" + ref : "");
  }
  if (cmd === "back" || cmd === "snapshot") return WEB_VERBS[cmd] || "Browsed";
  const text = arg("text") || arg("option") || arg("key") || arg("direction");
  return (
    (WEB_VERBS[cmd] || "Browsed") + (text ? ' "' + truncateArg(text) + '"' : "")
  );
}

function closeFileGroup() {
  if (!openFileGroup) return;
  openFileGroup.classList.remove("open");
  openFileGroup = null;
}

function resetToolRun() {
  toolRun = null;
}

function updateToolRunThinking() {
  const current = busy && toolRun && !toolRun.expanded ? toolRun.moreRow : null;
  if (thinkingRow && thinkingRow !== current) {
    thinkingRow.classList.remove("thinking");
    thinkingRow = null;
  }
  if (current && thinkingRow !== current) {
    current.classList.add("thinking");
    thinkingRow = current;
  }
}

const CHEV = '<span class="material-symbols-outlined chev">expand_more</span>';

function ensureMoreRow(run) {
  if (!run.moreRow) {
    const row = document.createElement("div");
    row.className = "see-more";
    row.innerHTML =
      '<button type="button" class="see-btn glass"><span class="see-label"><span class="l-more">See more</span>' +
      '<span class="l-less">See less</span></span><span class="see-slot">' +
      CHEV +
      '<span class="jump-dots"><i></i><i></i><i></i></span></span></button>';
    row
      .querySelector("button")
      .addEventListener("click", () =>
        run.expanded ? collapseToolRun(run) : expandToolRun(run),
      );
    run.moreRow = row;
  }
  return run.moreRow;
}

const CHAT_GAP = 10;

function ensureOverflowWrap(run) {
  if (!run.wrap) {
    const wrap = document.createElement("div");
    wrap.className = "tool-overflow collapsed";
    run.wrap = wrap;
    if (run.moreRow && run.moreRow.parentNode === chat) {
      chat.insertBefore(wrap, run.moreRow);
    } else if (statusRow.parentNode === chat) {
      chat.insertBefore(wrap, statusRow);
    } else {
      chat.appendChild(wrap);
    }
  }
  return run.wrap;
}

function expandToolRun(run) {
  run.expanded = true;
  Sfx.play("open");
  run.moreRow.classList.add("expanded");
  const stick = isAtBottom();
  const wrap = run.wrap;
  const rows = wrap ? Array.from(wrap.children) : [];
  if (wrap && !REDUCE) {
    cancelAnim(wrap);
    wrap.style.overflow = "hidden";
    rows.forEach((row) => cancelAnim(row));
    wrap.classList.remove("collapsed");
    const target = wrap.getBoundingClientRect().height;
    const anim = (wrap._anim = wrap.animate(
      [
        { height: "0px", marginTop: -CHAT_GAP + "px" },
        { height: target + "px", marginTop: "0px" },
      ],
      { duration: 520, easing: EASE_OUT, fill: "backwards" },
    ));
    anim.onfinish = () => {
      wrap._anim = null;
      if (!run.expanded) return;
      wrap.style.overflow = "";
    };
    rows.forEach((row, i) => {
      const fade = row.animate([{ opacity: 0 }, { opacity: 1 }], {
        duration: 600,
        delay: i * 80,
        easing: EASE_OUT,
        fill: "backwards",
      });
      row._anim = fade;
      fade.onfinish = () => fade.cancel();
    });
  } else if (wrap) {
    wrap.classList.remove("collapsed");
    rows.forEach((row) => cancelAnim(row));
  }
  updateToolRunThinking();
  if (stick) followBottom(900);
}

function collapseToolRun(run) {
  run.expanded = false;
  Sfx.play("close");
  run.moreRow.classList.remove("expanded");
  const stick = isAtBottom();
  const wrap = run.wrap;
  const rows = wrap ? Array.from(wrap.children) : [];
  if (wrap && !REDUCE) {
    rows.forEach((row) => {
      if (row._detailOpen) destroyDetail(row);
    });
    cancelAnim(wrap);
    wrap.classList.remove("collapsed");
    wrap.style.overflow = "hidden";
    rows.forEach((row) => {
      cancelAnim(row);
      row.style.opacity = "";
    });
    const start = wrap.getBoundingClientRect().height;
    const fades = rows.map(
      (row) =>
        (row._anim = row.animate([{ opacity: 1 }, { opacity: 0 }], {
          duration: 280,
          easing: EASE_IN,
          fill: "forwards",
        })),
    );
    Promise.all(fades.map((fade) => fade.finished.catch(() => {}))).then(() => {
      if (run.expanded) return;
      const anim = (wrap._anim = wrap.animate(
        [
          { height: start + "px", marginTop: "0px" },
          { height: "0px", marginTop: -CHAT_GAP + "px" },
        ],
        { duration: 420, easing: EASE_IN, fill: "forwards" },
      ));
      anim.onfinish = () => {
        wrap.classList.add("collapsed");
        wrap.style.overflow = "";
        wrap._anim = null;
        anim.cancel();
        rows.forEach((row) => {
          cancelAnim(row);
          row.style.opacity = "";
        });
      };
    });
  } else if (wrap) {
    rows.forEach((row) => {
      if (row._detailOpen) destroyDetail(row);
      cancelAnim(row);
    });
    wrap.classList.add("collapsed");
  }
  updateToolRunThinking();
  if (stick) followBottom(700);
}

function placeToolRow(row) {
  if (!toolRun) {
    toolRun = { rows: [], moreRow: null, expanded: false, wrap: null };
  }
  toolRun.rows.push(row);
  const stick = !replaying && isAtBottom();
  if (toolRun.rows.length > MAX_VISIBLE_TOOL_CALLS) {
    ensureMoreRow(toolRun);
    const wrap = ensureOverflowWrap(toolRun);
    if (toolRun.moreRow.parentNode !== chat) {
      chat.insertBefore(toolRun.moreRow, wrap.nextSibling);
      growIn(toolRun.moreRow);
    }
    wrap.appendChild(row);
    if (toolRun.expanded) growIn(row);
  } else {
    append(row);
  }
  if (stick) scrollToBottom();
  updateAmbient();
}

function addToolRow(event) {
  const meta = TOOL_META[event.name];
  const row = document.createElement("div");
  row.className =
    "tool-row glass " + (event.parse_error ? "failed" : "running");
  row.id = "tool-" + event.id;
  const icon = meta
    ? meta.icon
    : event.parse_error
      ? ICONS.warn
      : ICONS.terminal;
  row.innerHTML =
    '<span class="tool-icon">' +
    icon +
    "</span>" +
    '<span class="tool-text"></span>';
  row.querySelector(".tool-text").textContent = toolEventText(event);
  runningToolRow = row;
  placeToolRow(row);
}

let followUntil = 0,
  following = false;
function followBottom(ms) {
  followUntil = Math.max(followUntil, performance.now() + ms);
  if (following) return;
  following = true;
  (function step() {
    scrollToBottom();
    if (performance.now() < followUntil) requestAnimationFrame(step);
    else following = false;
  })();
}

const BOX_ZERO = {
  height: "0px",
  paddingTop: "0px",
  paddingBottom: "0px",
  borderTopWidth: "0px",
  borderBottomWidth: "0px",
};
const boxOf = (el, cs) => ({
  height: el.getBoundingClientRect().height + "px",
  paddingTop: cs.paddingTop,
  paddingBottom: cs.paddingBottom,
  borderTopWidth: cs.borderTopWidth,
  borderBottomWidth: cs.borderBottomWidth,
});

function growIn(el, delay) {
  if (REDUCE || replaying || !el.isConnected) return;
  /* A freshly inserted child of #chat is measured at its intrinsic-size
     placeholder until the browser's next rendering pass, so force a real
     render for the duration of the entrance animation. */
  el.classList.add("entering");
  if (!el.getBoundingClientRect().height) {
    el.classList.remove("entering");
    return;
  }
  const cs = getComputedStyle(el);
  const box = boxOf(el, cs);
  el.style.overflow = "hidden";
  const a = el.animate(
    [
      { ...BOX_ZERO, marginTop: -CHAT_ROW_GAP + "px" },
      { ...box, marginTop: cs.marginTop },
    ],
    { duration: 520, delay: delay || 0, easing: EASE_OUT, fill: "backwards" },
  );
  a.onfinish = a.oncancel = () => {
    el.style.overflow = "";
    el.classList.remove("entering");
  };
}

/* Mirror of growIn: collapse the element's box to zero height and margin,
   then remove it. onSettled runs whether the animation finishes or is
   cancelled, so callers can clear their tracking state. */
function shrinkOut(el, onSettled) {
  const settle = () => {
    if (onSettled) onSettled();
    el.remove();
  };
  if (
    REDUCE ||
    replaying ||
    !el.isConnected ||
    !el.getBoundingClientRect().height
  ) {
    settle();
    return;
  }
  const cs = getComputedStyle(el);
  const box = boxOf(el, cs);
  el.style.overflow = "hidden";
  const a = el.animate(
    [
      { ...box, marginTop: cs.marginTop },
      { ...BOX_ZERO, marginTop: -CHAT_ROW_GAP + "px" },
    ],
    { duration: 420, easing: EASE_IN, fill: "forwards" },
  );
  a.onfinish = a.oncancel = settle;
}

function detailSectionsFor(event) {
  const call = event.raw
    ? String(event.raw)
    : event.arguments
      ? JSON.stringify(event.arguments, null, 2)
      : "";
  const output = String(event.output || event.output_preview || "");
  const sections = [];
  const cap = (text) =>
    text.length > DETAIL_CAP_CHARS
      ? text.slice(0, DETAIL_CAP_CHARS) + "..."
      : text;
  if (call.trim()) sections.push({ label: "Call", text: cap(call) });
  if (output.trim()) sections.push({ label: "Output", text: cap(output) });
  return sections;
}

function detailSection(section) {
  const box = document.createElement("div");
  box.className = "detail-section";
  const header = document.createElement("div");
  header.className = "detail-header";
  const label = document.createElement("span");
  label.className = "detail-label";
  label.textContent = section.label;
  header.appendChild(label);
  addCopyButton(header, section.text);
  box.appendChild(header);
  const pre = document.createElement("pre");
  pre.textContent = section.text;
  box.appendChild(pre);
  return box;
}

function destroyDetail(row) {
  row._detailOpen = false;
  row.classList.remove("detail-open");
  if (row._closingDetail) {
    const closing = row._closingDetail;
    row._closingDetail = null;
    cancelAnim(closing);
    closing.remove();
  }
  if (row._detailEl) {
    cancelAnim(row._detailEl);
    row._detailEl.remove();
    row._detailEl = null;
  }
}

function closeDetail(row) {
  row._detailOpen = false;
  row.classList.remove("detail-open");
  const detail = row._detailEl;
  row._detailEl = null;
  if (!detail) return;
  const settle = () => {
    if (row._closingDetail === detail) row._closingDetail = null;
  };
  if (
    REDUCE ||
    replaying ||
    !detail.isConnected ||
    !detail.getBoundingClientRect().height
  ) {
    detail.remove();
    settle();
    return;
  }
  row._closingDetail = detail;
  shrinkOut(detail, settle);
}

function toggleDetail(row) {
  Sfx.play(row._detailOpen ? "close" : "open");
  if (row._detailOpen) {
    closeDetail(row);
    return;
  }
  if (row._closingDetail) {
    const closing = row._closingDetail;
    row._closingDetail = null;
    cancelAnim(closing);
    closing.remove();
  }
  const detail = document.createElement("div");
  detail.className = "tool-detail";
  (row._detailSections || []).forEach((section) =>
    detail.appendChild(detailSection(section)),
  );
  if (!detail.childElementCount) return;
  row._detailOpen = true;
  row.classList.add("detail-open");
  row._detailEl = detail;
  row.parentNode.insertBefore(detail, row.nextSibling);
  growIn(detail);
  const stick = isAtBottom();
  if (stick) followBottom(700);
}

function fitInput() {
  const prev = input.offsetHeight;
  input.style.transition = "none";
  input.style.height = "auto";
  const target = Math.min(input.scrollHeight, 160);
  input.style.height = prev + "px";
  void input.offsetHeight;
  input.style.transition = "";
  input.style.height = target + "px";
}

function finishToolRow(event) {
  let row = document.getElementById("tool-" + event.id);
  if (!row) {
    addToolRow(event);
    row = document.getElementById("tool-" + event.id);
  }
  row.classList.remove("running");
  if (runningToolRow === row) runningToolRow = null;
  row.classList.add("settling");
  setTimeout(() => row.classList.remove("settling"), 1200);
  if (!event.failed) row.classList.add("just-done");
  const icon = row.querySelector(".tool-icon");
  const text = row.querySelector(".tool-text");
  if (event.failed) {
    row.classList.add("failed");
    icon.innerHTML = ICONS.warn;
    text.textContent = "Tool call failed: " + toolEventText(event);
  } else {
    text.textContent = toolEventText(event);
  }
  row._detailSections = detailSectionsFor(event);
  row.classList.add("toggleable");
  if (!row._detailBound) {
    row._detailBound = true;
    row.addEventListener("click", () => toggleDetail(row));
  }
  updateAmbient();
}

function addFileChip(event) {
  closeFileGroup();
  const chipHost = document.createElement("div");
  chipHost.className =
    "files-group open" + (event.type === "file_sent" ? " right" : "");
  const chip = document.createElement("a");
  chip.className =
    "file-chip glass" + (event.type === "file_sent" ? " outgoing" : "");
  chip.href = event.url ? API_BASE + event.url : "#";
  if (event.url) chip.setAttribute("download", event.name);
  chip.innerHTML = ICONS.file + "<span></span><span class='file-meta'></span>";
  chip.children[1].textContent = event.name;
  chip.children[2].textContent = event.size ? formatSize(event.size) : "";
  chipHost.appendChild(chip);
  openFileGroup = chipHost;
  append(chipHost);
}

function addEventRow(text, isError) {
  const row = document.createElement("div");
  row.className = "event-row glass" + (isError ? " error" : "");
  row.textContent = text;
  append(row);
}

function addCompactionRow(event) {
  const row = document.createElement("div");
  row.className = "tool-row glass compaction-row toggleable";
  row.innerHTML =
    '<span class="tool-icon">' +
    ICONS.history +
    "</span>" +
    '<span class="tool-text">Summarized earlier history</span>';
  const summary = String(event.summary || "");
  row._detailSections = [
    {
      label: "Summary",
      text:
        summary.length > DETAIL_CAP_CHARS
          ? summary.slice(0, DETAIL_CAP_CHARS) + "..."
          : summary,
    },
  ];
  row.addEventListener("click", () => toggleDetail(row));
  placeToolRow(row);
}

let statusHideTimer = null;

function setStatus(kind, label) {
  statusDot.className = "status-dot " + kind;
  const widthBefore =
    statusRow.parentNode === chat ? statusRow.getBoundingClientRect().width : 0;
  statusLabel.textContent = label;
  if (widthBefore && !REDUCE) {
    const widthAfter = statusRow.getBoundingClientRect().width;
    if (widthAfter !== widthBefore) {
      statusRow.animate(
        [{ width: widthBefore + "px" }, { width: widthAfter + "px" }],
        { duration: 260, easing: EASE_OUT },
      );
    }
  }
  const show = kind === "working";
  if (show) {
    if (statusHideTimer) {
      clearTimeout(statusHideTimer);
      statusHideTimer = null;
    }
    statusRow.classList.remove("leaving");
    cancelAnim(statusRow);
    statusRow.style.overflow = "";
    if (statusRow.parentNode !== chat) {
      const stick = isAtBottom();
      chat.appendChild(statusRow);
      growIn(statusRow);
      if (stick) followBottom(700);
    }
  } else if (statusRow.parentNode === chat) {
    statusRow.classList.add("leaving");
    if (!REDUCE && !statusRow._anim) {
      const cs = getComputedStyle(statusRow);
      statusRow.style.overflow = "hidden";
      statusRow._anim = statusRow.animate(
        [
          { ...boxOf(statusRow, cs), marginTop: "0px" },
          { ...BOX_ZERO, marginTop: "-10px" },
        ],
        { duration: 480, easing: EASE_IN, fill: "forwards" },
      );
    }
    if (statusHideTimer) clearTimeout(statusHideTimer);
    statusHideTimer = setTimeout(() => {
      statusHideTimer = null;
      statusRow.classList.remove("leaving");
      cancelAnim(statusRow);
      statusRow.style.overflow = "";
      statusRow.remove();
    }, 480);
  }
}

function render(event) {
  const kick = {
    user_message: 1,
    model_message: 0.8,
    busy: 0.6,
    ready: 0.7,
    error: 1,
    tool_call: event.status === "started" ? 0.35 : 0.5,
  }[event.type];
  if (kick && window.ambientKick) window.ambientKick(kick);
  const snd = {
    model_message: "receive",
    error: "error",
    tool_call:
      event.status === "started" ? "tick" : event.failed ? "error" : "done",
  }[event.type];
  const narrates =
    event.type === "model_message" &&
    window.Voice &&
    Voice.willNarrate(event.content);
  if (snd && !narrates) Sfx.play(snd);
  if (event.type !== "file" && event.type !== "file_sent") closeFileGroup();
  if (
    event.type !== "tool_call" &&
    event.type !== "status" &&
    event.type !== "busy" &&
    event.type !== "ready" &&
    event.type !== "compaction"
  ) {
    resetToolRun();
  }
  switch (event.type) {
    case "user_message":
      addBubble(event.content, "user", event.queued);
      break;
    case "model_message":
      addBubble(event.content, "model", false);
      if (!replaying && window.Voice) {
        if (narrates) Voice.showPill("Tuning up, please wait...");
        Voice.speak(event.content);
      }
      break;
    case "history_done":
      replaying = false;
      break;
    case "tool_call":
      if (event.status === "started") addToolRow(event);
      else finishToolRow(event);
      break;
    case "file":
    case "file_sent":
      addFileChip(event);
      break;
    case "compaction":
      addCompactionRow(event);
      break;
    case "status":
      if (!event.quiet) setStatus(busy ? "working" : "ready", event.text);
      break;
    case "busy":
      busy = true;
      setStatus("working", "Working...");
      setSendState();
      updateToolRunThinking();
      updateAmbient();
      break;
    case "ready":
      busy = false;
      setStatus("ready", "Ready");
      setSendState();
      updateToolRunThinking();
      updateAmbient();
      break;
    case "error":
      addEventRow(event.text, true);
      setStatus("error", "Error");
      updateAmbient();
      break;
  }
}

function connect() {
  ws = new WebSocket(WS_BASE + "/ws/" + clientId);

  ws.onopen = () => {
    reconnectDelay = 1000;
    banner.classList.remove("visible");
    chat.innerHTML = "";
    toolRun = null;
    replaying = true;
    replayQueue = [];
    replayFlushPending = false;
    runningToolRow = null;
    openFileGroup = null;
    thinkingRow = null;
    if (window.Voice) Voice.reset();
    setStatus("ready", "Ready");
    updateAmbient();
  };

  ws.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data);
      if (replaying) {
        replayQueue.push(event);
        scheduleReplayFlush();
      } else {
        render(event);
      }
    } catch (err) {
      console.error(err);
    }
  };

  ws.onclose = () => {
    busy = false;
    setSendState();
    setStatus("", "Connecting...");
    updateAmbient();
    banner.classList.add("visible");
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 10000);
  };

  ws.onerror = () => ws.close();
}

function setSendState() {
  const mode = busy ? "stop" : "send";
  if (sendBtn.dataset.mode !== mode) {
    sendBtn.dataset.mode = mode;
    sendBtn.classList.remove("pop");
    void sendBtn.offsetWidth;
    sendBtn.classList.add("pop");
    sendBtn.innerHTML = busy ? ICONS.stop : ICONS.send;
    sendBtn.title = busy ? "Stop" : "Send";
  }
  document.querySelector(".composer").classList.toggle("busy", busy);
  sendBtn.disabled = busy ? false : !input.value.trim();
}

function sendMessage() {
  const content = input.value.trim();
  if (!content || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "message", content: content }));
  Sfx.play("send");
  input.value = "";
  fitInput();
  setSendState();
  input.focus();
}

function sendStop() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "stop" }));
  setStatus("working", "Stopping...");
}

let newChatBusy = false;

async function newChat() {
  if (newChatBusy) return;
  newChatBusy = true;
  newchatBtn.disabled = true;
  Sfx.play("click");
  setStatus("working", "Cleaning up, please wait...");
  addEventRow("Cleaning up the pod, please wait...");
  try {
    if (busy && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "stop" }));
    }
    const response = await fetch(API_BASE + "/api/new-chat/" + clientId, {
      method: "POST",
    });
    if (!response.ok) throw new Error("HTTP " + response.status);
    if (ws) {
      ws.onclose = null;
      ws.close();
      ws = null;
    }
    chat.innerHTML = "";
    toolRun = null;
    replayQueue = [];
    runningToolRow = null;
    openFileGroup = null;
    thinkingRow = null;
    busy = false;
    setSendState();
    addEventRow("Starting a fresh session...");
    input.focus();
    connect();
  } catch (err) {
    addEventRow("Could not start a new chat: " + err.message, true);
    setStatus("ready", "Ready");
  } finally {
    newChatBusy = false;
    newchatBtn.disabled = false;
  }
}

function uploadFiles(files) {
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    fetch(API_BASE + "/api/upload/" + clientId, { method: "POST", body: form })
      .then((response) => {
        if (!response.ok)
          throw new Error("Upload failed (" + response.status + ")");
      })
      .catch((err) =>
        addEventRow("Could not send " + file.name + ": " + err.message, true),
      );
  }
}

attachBtn.innerHTML = ICONS.clip;
newchatBtn.innerHTML = ICONS.newchat;
setSendState();
input.focus();

sendBtn.addEventListener("click", () =>
  busy ? (Sfx.play("stop"), sendStop()) : sendMessage(),
);
attachBtn.addEventListener("click", () => fileInput.click());
newchatBtn.addEventListener("click", newChat);
fileInput.addEventListener("change", () => {
  uploadFiles(fileInput.files);
  fileInput.value = "";
});

input.addEventListener("input", () => {
  fitInput();
  setSendState();
});
input.addEventListener("keydown", (keyEvent) => {
  if (keyEvent.key === "Enter" && !keyEvent.shiftKey) {
    keyEvent.preventDefault();
    sendMessage();
  }
});

let dragDepth = 0;

function transferHasFiles(dataTransfer) {
  return dataTransfer && Array.from(dataTransfer.types).includes("Files");
}

function uploadDroppedFiles(dataTransfer) {
  const files = Array.from(dataTransfer.files).filter(
    (file) => file.size > 0 || file.type,
  );
  if (files.length) uploadFiles(files);
}

window.addEventListener("dragenter", (event) => {
  if (!transferHasFiles(event.dataTransfer)) return;
  event.preventDefault();
  dragDepth += 1;
  document.body.classList.add("dragging");
});

window.addEventListener("dragover", (event) => {
  if (!transferHasFiles(event.dataTransfer)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
});

window.addEventListener("dragleave", (event) => {
  if (!transferHasFiles(event.dataTransfer)) return;
  event.preventDefault();
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) document.body.classList.remove("dragging");
});

window.addEventListener("drop", (event) => {
  dragDepth = 0;
  document.body.classList.remove("dragging");
  if (!transferHasFiles(event.dataTransfer)) return;
  event.preventDefault();
  uploadDroppedFiles(event.dataTransfer);
});

input.addEventListener("paste", (event) => {
  if (!event.clipboardData || !event.clipboardData.files.length) return;
  event.preventDefault();
  uploadFiles(event.clipboardData.files);
});

connect();
