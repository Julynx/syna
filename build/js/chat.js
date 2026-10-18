/* ==== js/chat.js ==== */
"use strict";

// Chat client: WebSocket transport, conversation rendering (bubbles, tool rows,
// file chips, events, compaction) and the composer. Visual state classes are
// styled in css/chat.css; all entrance/exit motion runs through the WAAPI
// helpers below so nothing on the page ever snaps.

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

const REDUCE = matchMedia("(prefers-reduced-motion: reduce)").matches;
const EASE_OUT = "cubic-bezier(0.22, 1, 0.36, 1)";
const EASE_IN = "cubic-bezier(0.65, 0, 0.35, 1)";

// ---------- icons ----------
// Glyphs from the vendored Material Symbols Outlined subset (see base.css for
// the @font-face and the regeneration instructions). Icons that get
// stacked/crossfaded inside a button take a class (cls).
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
  mic: GLYPH("mic"),
  speakerOn: GLYPH("record_voice_over", "s-on"),
  speakerOff: GLYPH("voice_over_off", "s-off"),
};

const TOOL_META = {
  execute_command: { verb: "Ran command", icon: ICONS.terminal, arg: 0 },
  web_interact: { verb: "Browsed", icon: ICONS.globe, arg: 0 },
  web_search: { verb: "Searched", icon: ICONS.search, arg: 0 },
  read_file: { verb: "Read file", icon: ICONS.eye, arg: 0 },
  edit_file: { verb: "Edited file", icon: ICONS.pencil, arg: 0 },
  share_file: { verb: "Share file", icon: ICONS.share, arg: 0 },
};

const DECODE_ERROR_PREFIX = "A tool call could not be decoded as JSON";

// ---------- small helpers ----------
function formatSize(bytes) {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + " KB";
  return bytes + " B";
}

function firstQuotedArg(signature) {
  const text = String(signature);
  const quoted = text.match(/'((?:[^'\\]|\\.)*)'/);
  if (quoted) return quoted[1].replace(/\\'/g, "'").replace(/\\\\/g, "\\");
  const bare = text.match(/=\s*([^,)]+)/);
  return bare ? bare[1].trim() : "";
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
  const stick = isAtBottom();
  if (statusRow.parentNode === chat) chat.insertBefore(element, statusRow);
  else chat.appendChild(element);
  growIn(element);
  if (stick) followBottom(700);
}

// Cancels a stored WAAPI animation (rows, panels and the status pill keep
// their running animation on el._anim).
function cancelAnim(host) {
  if (host._anim) {
    host._anim.onfinish = null;
    host._anim.cancel();
    host._anim = null;
  }
}

// ---------- bubbles ----------
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

// ---------- copy-markdown button (top right of every model message) ----------
// Copies the model's raw markdown source — never the rendered HTML.
const COPY_FEEDBACK_MS = 1400;

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext)
    return navigator.clipboard.writeText(text);
  // Fallback for non-secure contexts (e.g. plain http over the network)
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

// ---------- tool rows ----------
function toolEventText(event) {
  const meta = TOOL_META[event.name];
  if (!meta) return truncateArg(event.signature);
  if (event.name === "web_interact") return webInteractText(event.signature);
  const arg = truncateArg(firstQuotedArg(event.signature));
  let verb = meta.verb;
  if (
    event.name === "web_search" &&
    /^https?:\/\//.test(firstQuotedArg(event.signature))
  )
    verb = "Visited";
  return verb + (arg ? ' "' + arg + '"' : "");
}

// web_interact rows read as actions, not raw signatures: the browser
// command picks the verb and the most meaningful argument is shown.
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
};

function webInteractText(signature) {
  const cmd = firstQuotedArg(signature);
  const arg = (key) => {
    const match = signature.match(new RegExp("'" + key + "':\\s*'([^']*)'"));
    return match ? match[1] : null;
  };
  if (cmd === "goto") return 'Visited "' + truncateArg(arg("url") || "") + '"';
  if (cmd === "click" || cmd === "read") {
    const ref = signature.match(/'ref':\s*(\d+)/);
    return WEB_VERBS[cmd] + (ref ? " #" + ref[1] : "");
  }
  if (cmd === "back" || cmd === "snapshot") return WEB_VERBS[cmd] || "Browsed";
  const text =
    arg("text") ||
    arg("option") ||
    arg("key") ||
    (signature.match(/'direction':\s*'([^']*)'/) || [])[1];
  return (
    (WEB_VERBS[cmd] || "Browsed") + (text ? ' "' + truncateArg(text) + '"' : "")
  );
}

function closeFileGroup() {
  const group = chat.querySelector(".files-group.open");
  if (group) group.classList.remove("open");
}

function resetToolRun() {
  toolRun = null;
}

function updateToolRunThinking() {
  chat.querySelectorAll(".see-more.thinking").forEach((row) => {
    if (!(toolRun && toolRun.moreRow === row) || !busy || toolRun.expanded) {
      row.classList.remove("thinking");
    }
  });
  if (busy && toolRun && !toolRun.expanded && toolRun.moreRow) {
    toolRun.moreRow.classList.add("thinking");
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

// ---------- see more / less (structural fix) ----------
// Every row past MAX_VISIBLE_TOOL_CALLS lives inside a .tool-overflow wrapper
// sitting between the visible rows and the pill. Expanding/collapsing animates
// THE WRAPPER's height (one element = one layout change per frame), while the
// rows inside only crossfade — opacity and blur never trigger layout, so the
// motion stays on the compositor and nothing around the run snaps. Collapsed,
// the wrapper's negative margin cancels #chat's row gap so the spacing is
// exactly what display:none rows used to leave behind.

const CHAT_GAP = 10; // keep in sync with #chat { gap: 10px } and .tool-overflow { gap }

function ensureOverflowWrap(run) {
  if (!run.wrap) {
    const wrap = document.createElement("div");
    wrap.className = "tool-overflow collapsed";
    run.wrap = wrap;
    if (run.moreRow && run.moreRow.parentNode === chat) {
      chat.insertBefore(wrap, run.moreRow);
    } else if (statusRow.parentNode === chat) {
      chat.insertBefore(wrap, statusRow); // status pill always stays last
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
    wrap.classList.remove("collapsed");
    // Fractional target from the laid-out element (scrollHeight rounds to
    // whole pixels and would snap against the natural height at the end),
    // mirroring the collapse path's start measurement.
    const target = wrap.getBoundingClientRect().height;
    const anim = (wrap._anim = wrap.animate(
      [
        { height: "0px", marginTop: -CHAT_GAP + "px" },
        { height: target + "px", marginTop: "0px" },
      ],
      { duration: 520, easing: EASE_OUT, fill: "backwards" },
    ));
    anim.onfinish = () => {
      wrap.style.overflow = "";
      wrap._anim = null;
    };
    rows.forEach((row, i) => {
      cancelAnim(row); // a lingering collapse fade would otherwise keep opacity at 0
      row.style.opacity = "0";
      const fade = row.animate(
        [
          { opacity: 0, filter: "blur(6px)" },
          { opacity: 1, filter: "blur(0px)" },
        ],
        {
          duration: 360,
          delay: Math.min(i * 55, 280),
          easing: EASE_OUT,
          fill: "backwards",
        },
      );
      fade.onfinish = () => {
        row.style.opacity = "";
      };
    });
  } else if (wrap) {
    wrap.classList.remove("collapsed");
    rows.forEach((row) => {
      cancelAnim(row);
      row.style.opacity = "";
    });
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
    cancelAnim(wrap);
    wrap.style.overflow = "hidden";
    const start = wrap.getBoundingClientRect().height;
    const anim = (wrap._anim = wrap.animate(
      [
        { height: start + "px", marginTop: "0px" },
        { height: "0px", marginTop: -CHAT_GAP + "px" },
      ],
      { duration: 420, easing: EASE_IN, fill: "forwards" },
    ));
    rows.forEach((row) => {
      cancelAnim(row);
      row._anim = row.animate(
        [
          { opacity: 1, filter: "blur(0px)" },
          { opacity: 0, filter: "blur(6px)" },
        ],
        { duration: 280, easing: EASE_IN, fill: "forwards" },
      );
    });
    anim.onfinish = () => {
      wrap.classList.add("collapsed");
      wrap.style.overflow = "";
      wrap._anim = null;
      anim.cancel(); // .collapsed holds the end state; drop the fill
      rows.forEach((row) => cancelAnim(row)); // drop the forwards fills too
    };
  } else if (wrap) {
    wrap.classList.add("collapsed");
    rows.forEach((row) => cancelAnim(row));
  }
  updateToolRunThinking();
  if (stick) followBottom(700);
}

// Shared placement for every tool row, successful or failed: rows always join
// the current run and count toward the MAX_VISIBLE_TOOL_CALLS cap, so errored
// calls are grouped behind the see-more pill exactly like the rest.
function placeToolRow(row) {
  if (!toolRun) {
    toolRun = { rows: [], moreRow: null, expanded: false, wrap: null };
  }
  toolRun.rows.push(row);
  const stick = isAtBottom();
  if (toolRun.rows.length > MAX_VISIBLE_TOOL_CALLS) {
    // Rows past the cap always live in the wrapper. Collapsed, the wrapper
    // keeps its height at 0, so adding rows shifts nothing outside it.
    ensureMoreRow(toolRun);
    const wrap = ensureOverflowWrap(toolRun);
    if (toolRun.moreRow.parentNode !== chat) {
      chat.insertBefore(toolRun.moreRow, wrap.nextSibling);
      growIn(toolRun.moreRow); // the pill must not pop in unanimated
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
  row.className = "tool-row glass running";
  row.id = "tool-" + event.id;
  const icon = meta ? meta.icon : ICONS.terminal;
  row.innerHTML =
    '<span class="tool-icon">' +
    icon +
    "</span>" +
    '<span class="tool-text"></span>' +
    "<pre hidden></pre>";
  row.querySelector(".tool-text").textContent = toolEventText(event);
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

// New elements grow from zero height so nothing around them snaps.
function growIn(el, delay) {
  if (REDUCE || !el.isConnected || !el.getBoundingClientRect().height) return;
  const cs = getComputedStyle(el);
  const gap = parseFloat(getComputedStyle(chat).rowGap) || 0;
  el.style.overflow = "hidden";
  const a = el.animate(
    [
      { ...BOX_ZERO, marginTop: -gap + "px" },
      { ...boxOf(el, cs), marginTop: cs.marginTop },
    ],
    { duration: 520, delay: delay || 0, easing: EASE_OUT, fill: "backwards" },
  );
  a.onfinish = () => {
    el.style.overflow = "";
  };
}

// Tool-call details: the output panel animates height + margin + fade, so the
// row (and everything below it) resizes continuously.
function toggleOpen(row) {
  const pre = row.querySelector("pre");
  if (!pre || REDUCE) {
    Sfx.play(row.classList.contains("open") ? "close" : "open");
    row.classList.toggle("open");
    return;
  }
  cancelAnim(pre);
  const opening = !row.classList.contains("open");
  Sfx.play(opening ? "open" : "close");
  const gap = parseFloat(getComputedStyle(row).rowGap) || 0;
  if (opening) row.classList.add("open");
  pre.style.overflow = "hidden";
  const cs = getComputedStyle(pre);
  const shown = {
    ...boxOf(pre, cs),
    marginTop: cs.marginTop,
    opacity: 1,
    filter: "blur(0px)",
    transform: "none",
  };
  const hidden = {
    ...BOX_ZERO,
    marginTop: -gap + "px",
    opacity: 0,
    filter: "blur(8px)",
    transform: "translateY(3px)",
  };
  const anim = (pre._anim = pre.animate(
    opening ? [hidden, shown] : [shown, hidden],
    {
      duration: opening ? 520 : 380,
      easing: opening ? EASE_OUT : EASE_IN,
      fill: opening ? "backwards" : "forwards",
    },
  ));
  anim.onfinish = () => {
    if (!opening) row.classList.remove("open");
    pre.style.overflow = "";
    anim.cancel();
    pre._anim = null;
  };
  const stick = isAtBottom();
  if (stick) followBottom(opening ? 560 : 420);
}

// textarea grows/shrinks with a transition (height can't animate from auto)
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
  const preview = (event.output_preview || "").trim();
  if (preview) {
    row.classList.add("toggleable");
    row.addEventListener("click", () => toggleOpen(row));
    const pre = row.querySelector("pre");
    pre.hidden = false;
    pre.textContent = preview;
  }
  updateAmbient();
}

function addDecodeErrorRow(detail) {
  const row = document.createElement("div");
  row.className = "tool-row glass failed toggleable";
  row.innerHTML =
    '<span class="tool-icon">' +
    ICONS.warn +
    "</span>" +
    '<span class="tool-text">Tool call failed: "' +
    DECODE_ERROR_PREFIX +
    '"</span>' +
    "<pre hidden></pre>";
  row.querySelector("pre").textContent = detail;
  row.addEventListener("click", () => toggleOpen(row));
  placeToolRow(row); // count errored calls toward the run's grouping cap
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
  append(chipHost);
}

function addEventRow(text, isError) {
  const row = document.createElement("div");
  row.className = "event-row glass" + (isError ? " error" : "");
  row.textContent = text;
  append(row);
}

function addCompactionRow(event) {
  const divider = document.createElement("div");
  divider.className = "compaction-divider";
  divider.textContent = "----- Compaction -----";
  append(divider);
  const box = document.createElement("pre");
  box.className = "compaction-summary glass";
  box.textContent = event.summary;
  append(box);
}

let statusHideTimer = null;

function setStatus(kind, label) {
  statusDot.className = "status-dot " + kind;
  // The pill hugs its text (width: fit-content): a bare text swap would
  // snap its width, so animate old width -> new width while it is visible.
  const widthBefore =
    statusRow.parentNode === chat
      ? statusRow.getBoundingClientRect().width
      : 0;
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

// ---------- event dispatch ----------
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
  // A narrated message speaks instead of blipping: only the voice is heard
  // while the model talks.
  const narrates =
    event.type === "model_message" &&
    window.Voice &&
    Voice.willNarrate(event.content);
  if (snd && !narrates) Sfx.play(snd);
  if (event.type !== "file" && event.type !== "file_sent") closeFileGroup();
  // A decode error is a malformed reply of the ongoing turn, not a new one:
  // keep the run alive so its errored row groups with the surrounding calls.
  const isDecodeError =
    event.type === "error" && event.text.startsWith(DECODE_ERROR_PREFIX);
  if (
    event.type !== "tool_call" &&
    event.type !== "status" &&
    event.type !== "busy" &&
    event.type !== "ready" &&
    !isDecodeError
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
      setStatus(busy ? "working" : "ready", event.text);
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
      if (event.text.startsWith(DECODE_ERROR_PREFIX)) {
        addDecodeErrorRow(event.text);
      } else {
        addEventRow(event.text, true);
      }
      setStatus("error", "Error");
      updateAmbient();
      break;
  }
}

// ---------- transport ----------
function connect() {
  ws = new WebSocket(WS_BASE + "/ws/" + clientId);

  ws.onopen = () => {
    reconnectDelay = 1000;
    banner.classList.remove("visible");
    chat.innerHTML = "";
    toolRun = null;
    replaying = true;
    if (window.Voice) Voice.reset(); // every fresh session starts voice as configured
    setStatus("ready", "Ready");
    updateAmbient();
  };

  ws.onmessage = (message) => {
    try {
      render(JSON.parse(message.data));
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

// ---------- composer ----------
function setSendState() {
  // Icon swap (and the .pop animation) only on a send<->stop mode change,
  // which happens when a message is sent or a turn finishes. Typing must
  // not retrigger the animation: rewriting innerHTML on every keystroke
  // would insert a fresh icon that replays the pop while .pop lingers.
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

// ---------- new chat ----------
// Stops the running turn (the server does the same if it is still busy),
// wipes the VM + conversation server-side, then reconnects for a fresh
// session with a brand new container. Teardown is not instant — stopping a
// turn alone can wait up to 20s server-side, plus container cleanup — so
// the button disables itself and the client says what is going on instead
// of sitting silent on a clickable button.
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
      ws.send(JSON.stringify({ type: "stop" })); // stop-then-wipe
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
    busy = false;
    setSendState();
    addEventRow("Starting a fresh session...");
    input.focus();
    connect(); // onopen clears the rest of the visual state
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

// ---------- wiring & boot ----------
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

// ---------- drag & drop / paste uploads ----------
// Files dragged over the window or pasted into the composer take the same
// upload path as the attach button. The drop overlay is revealed while a file
// drag is over the page; a depth counter keeps it stable across child
// element boundaries.

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
