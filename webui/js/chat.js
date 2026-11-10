"use strict";

const chat = document.getElementById("chat");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const attachBtn = document.getElementById("attach-btn");
const allegroBtn = document.getElementById("allegro-btn");
const newchatBtn = document.getElementById("newchat-btn");
const settingsBtn = document.getElementById("settings-btn");
const bgBtn = document.getElementById("bg-btn");
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
const AUTH_TOKEN = (window.SYNA_CONFIG && window.SYNA_CONFIG.auth_token) || "";
const APP_VERSION =
  (window.SYNA_CONFIG && window.SYNA_CONFIG.version) || "";

/* The per-process server token rides along on every authenticated call:
   headers for fetches, the query string for direct links (<img>, file
   downloads) and the WebSocket, which cannot set headers. */
function withToken(url) {
  if (!AUTH_TOKEN) return url;
  return (
    url +
    (url.includes("?") ? "&" : "?") +
    "token=" +
    encodeURIComponent(AUTH_TOKEN)
  );
}

function authHeaders(extra) {
  return AUTH_TOKEN
    ? { ...(extra || {}), "X-Syna-Token": AUTH_TOKEN }
    : extra || {};
}

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
  settings: GLYPH("tune"),
  close: GLYPH("close"),
  reset: GLYPH("restart_alt"),
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
  download: GLYPH("download", "i-download"),
  undo: GLYPH("undo", "i-undo"),
  history: GLYPH("history"),
  mic: GLYPH("mic", "m-on"),
  micOff: GLYPH("mic_off", "m-off"),
  think: GLYPH("psychology"),
  image: GLYPH("image"),
  help: GLYPH("help"),
  factCheck: GLYPH("fact_check"),
  speakerOn: GLYPH("record_voice_over", "s-on"),
  speakerOff: GLYPH("voice_over_off", "s-off"),
  allegro: GLYPH("queue_music"),
  subagent: GLYPH("account_tree"),
  schedule: GLYPH("schedule"),
  work: GLYPH("list_alt"),
};

const allegro = (() => {
  let on = false;
  try {
    on = localStorage.getItem("syna-allegro") === "on";
  } catch (e) {}
  allegroBtn.innerHTML =
    ICONS.allegro + '<span class="allegro-label">Allegro</span>';
  const paint = () => {
    allegroBtn.classList.toggle("on", on);
    allegroBtn.setAttribute("aria-pressed", String(on));
  };
  paint();
  allegroBtn.addEventListener("click", () => {
    on = !on;
    try {
      localStorage.setItem("syna-allegro", on ? "on" : "off");
    } catch (e) {}
    paint();
    allegroBtn.classList.remove("pop");
    void allegroBtn.offsetWidth;
    allegroBtn.classList.add("pop");
    Sfx.play(on ? "allegroOn" : "allegroOff");
  });
  return {
    get on() {
      return on;
    },
  };
})();

const TOOL_META = {
  think: { verb: "Noted", icon: ICONS.think, key: "thought" },
  execute_command: {
    verb: "Ran command",
    icon: ICONS.terminal,
    key: "command",
  },
  browser: { verb: "Browsed", icon: ICONS.globe },
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
  ask_user: { verb: "Asked you", icon: ICONS.help, key: "question_text" },
  review_work: {
    verb: "Verifying work",
    icon: ICONS.factCheck,
    key: "summary",
  },
  process: {
    verb: "Process",
    icon: ICONS.terminal,
  },
  subagent: {
    verb: "Subagent",
    icon: ICONS.subagent,
  },
  wait_for: { verb: "Waited for", icon: ICONS.schedule, key: "name" },
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
  const args = event.arguments || null;
  if (args && meta && meta.key) {
    const value = args[meta.key];
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

function addBubble(content, side, msgId) {
  const bubble = document.createElement("div");
  bubble.className = "bubble " + side;
  const rendered = side === "model" ? renderMarkdown(content) : "";
  if (rendered) {
    bubble.classList.add("markdown");
    bubble.innerHTML = rendered;
    applyLinkPolicy(bubble);
    applyImagePolicy(bubble);
    renderMath(bubble);
  } else {
    bubble.textContent = content;
  }
  if (side === "model") {
    addDownloadButton(bubble, content);
    addCopyButton(bubble, content);
  } else if (side === "user" && msgId) {
    bubble.dataset.msgId = msgId;
    addRewindButton(bubble, msgId);
  }
  append(bubble);
}

/* Rewind: the server streams rewind_state deltas (added/removed message
   ids plus pod availability); a bubble whose id is on the live list gets
   an undo button that opens the revert-choice modal. */
let rewindAvailable = false;
const rewindIds = new Set();

function rewindEligible(id) {
  return rewindAvailable && rewindIds.has(id);
}

function addRewindButton(bubble, id) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-btn rewind-btn glass";
  btn.title = "Rewind to here";
  btn.setAttribute("aria-label", "Rewind to this message");
  btn.innerHTML = ICONS.undo;
  btn.hidden = !rewindEligible(id);
  btn.addEventListener("click", (event) => {
    event.stopPropagation();
    openRewindModal(id);
  });
  bubble.appendChild(btn);
  return btn;
}

function applyRewindState() {
  chat.querySelectorAll(".bubble.user").forEach((bubble) => {
    const id = bubble.dataset.msgId;
    if (!id) return;
    const btn = bubble.querySelector(".rewind-btn");
    if (rewindEligible(id)) {
      if (btn) btn.hidden = false;
      else addRewindButton(bubble, id);
    } else if (btn) {
      btn.remove();
    }
  });
}

function sendRewind(id, mode) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "rewind", id: id, mode: mode }));
  if (busy) setStatus("working", "Stopping...");
}

function openRewindModal(id) {
  Sfx.play("click");
  const backdrop = document.createElement("div");
  backdrop.className = "rewind-modal-backdrop";
  const modal = document.createElement("div");
  modal.className = "rewind-modal";
  const title = document.createElement("h3");
  title.className = "rewind-modal-title";
  title.textContent = "Choose what to revert";
  modal.appendChild(title);
  const close = () => backdrop.remove();
  const choice = (label, action, className = "rewind-choice") => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = className;
    btn.textContent = label;
    btn.addEventListener("click", () => {
      close();
      if (action) action();
    });
    return btn;
  };
  modal.appendChild(
    choice("Messages only", () => sendRewind(id, "messages"), "rewind-choice danger"),
  );
  modal.appendChild(
    choice("Messages & workspace", () => sendRewind(id, "all"), "rewind-choice danger"),
  );
  modal.appendChild(choice("Cancel", null));
  backdrop.appendChild(modal);
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) close();
  });
  document.body.appendChild(backdrop);
  requestAnimationFrame(() => backdrop.classList.add("open"));
}

/* Apply one history_rewind event: drop the chosen user bubble and
   everything after it, clear the queue dock, and refill the input with
   the rewound message so it can be edited and re-sent (or deleted). */
function rewindTranscript(id, content) {
  const bubble = chat.querySelector(
    '.bubble.user[data-msg-id="' + CSS.escape(id) + '"]',
  );
  if (bubble) {
    let node = bubble;
    while (node) {
      const next = node.nextSibling;
      node.remove();
      node = next;
    }
  }
  clearQueuedMessages();
  resetAskState();
  toolRun = null;
  runningToolRow = null;
  openFileGroup = null;
  thinkingRow = null;
  Sfx.endToolWait();
  input.value = content;
  fitInput();
  setSendState();
  scrollToBottom();
  if (!replaying) input.focus();
}

/* Images from the pod render inline through the session's image endpoint;
   remote URLs are never fetched automatically: a page could otherwise
   make Syna embed an image whose URL exfiltrates data. They become links. */
function applyImagePolicy(bubble) {
  bubble.querySelectorAll("img").forEach((img) => {
    const src = img.getAttribute("src") || "";
    if (src.startsWith("/")) {
      let podPath = src;
      try {
        podPath = decodeURIComponent(src);
      } catch (err) {
        podPath = src;
      }
      /* The buster is not HTTP caching: the endpoint sends no-store
         already. Chromium's memory cache reuses a same-URL image in the
         same tab without honoring it, which served a stale image when a
         message embedded a regenerated file under the same name. A
         unique query per render makes every embed a distinct URL; the
         endpoint copies from the pod per request, so each one is fresh. */
      const url = withToken(
        API_BASE +
          "/api/image/" +
          clientId +
          "?path=" +
          encodeURIComponent(podPath) +
          "&v=" +
          Date.now(),
      );
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = img.alt || "Image from the pod";
      img.addEventListener("error", () => {
        const chip = document.createElement("span");
        chip.className = "image-fallback";
        chip.textContent = podPath;
        const frame = img.closest(".inline-image-frame");
        (frame || img).replaceWith(chip);
      });
      addImageDownloadButton(img, url);
      img.src = url;
      return;
    }
    if (!src.startsWith("data:")) {
      const link = document.createElement("a");
      link.href = src;
      link.textContent = src;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      img.replaceWith(link);
    }
  });
}

function addImageDownloadButton(img, url) {
  const frame = document.createElement("div");
  frame.className = "inline-image-frame";
  img.replaceWith(frame);
  frame.appendChild(img);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-btn glass image-download";
  btn.title = "Download image";
  btn.setAttribute("aria-label", "Download image");
  btn.innerHTML = ICONS.download + ICONS.check;
  btn.addEventListener("click", (event) => {
    event.stopPropagation();
    const anchor = document.createElement("a");
    anchor.href = url + "&download=1";
    anchor.download = "";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    btn.classList.add("copied");
    clearTimeout(btn._t);
    btn._t = setTimeout(() => btn.classList.remove("copied"), COPY_FEEDBACK_MS);
  });
  frame.appendChild(btn);
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

function downloadMarkdown(text) {
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  const blob = new Blob([text], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "syna-message-" + stamp + ".md";
  anchor.click();
  URL.revokeObjectURL(url);
}

function addDownloadButton(bubble, source) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-btn download-btn glass";
  btn.title = "Download markdown";
  btn.setAttribute("aria-label", "Download message as markdown file");
  btn.innerHTML = ICONS.download + ICONS.check;
  btn.addEventListener("click", () => {
    try {
      downloadMarkdown(source);
      btn.classList.add("copied");
      clearTimeout(btn._t);
      btn._t = setTimeout(
        () => btn.classList.remove("copied"),
        COPY_FEEDBACK_MS,
      );
    } catch (err) {}
  });
  bubble.appendChild(btn);
}

function toolEventText(event) {
  if (event.parse_error) return "invalid tool call format";
  const meta = TOOL_META[event.name];
  if (!meta) return truncateArg(event.signature);
  if (event.name === "browser") return browserEventText(event);
  if (event.name === "process" || event.name === "subagent")
    return dispatchEventText(event);
  const arg = truncateArg(primaryArg(event));
  let verb = meta.verb;
  return verb + (arg ? ' "' + arg + '"' : "");
}

/* The dispatch tools carry their action and the background work's name
   one level down (`{action, args}`), and a spawn's auto-generated name
   only exists once the spawn result arrives (structured_output), so the
   label starts bare and gains the name when the row settles. Events
   recorded before the action rename (`{command, args}`) fall back to
   their `command` value, which named the same actions. */
function dispatchEventText(event) {
  const args = event.arguments || null;
  const nested =
    args && args.args && typeof args.args === "object" ? args.args : {};
  const action =
    args && args.action != null
      ? String(args.action)
      : args && args.command != null
        ? String(args.command)
        : (String(event.signature).match(/\baction= *'([^']*)'/) || [])[1] ||
          "";
  let name = nested.name ? String(nested.name) : "";
  if (!name && args && args.name != null) name = String(args.name);
  if (!name && event.structured_output && event.structured_output.name)
    name = String(event.structured_output.name);
  if (!name) {
    const match = String(event.signature).match(/'name':\s*'([^']*)'/);
    if (match) name = match[1];
  }
  return bgCallLabel(event.name, action, truncateArg(name));
}

const WEB_VERBS = {
  visit: "Visited",
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

function browserEventText(event) {
  const args = event.arguments || null;
  const signature = String(event.signature);
  const cmd = args ? String(args.command || "") : firstQuotedArg(signature);
  const arg = (key) => {
    if (args && args[key] !== undefined && args[key] !== null)
      return String(args[key]);
    const match = signature.match(new RegExp("'" + key + "':\\s*'([^']*)'"));
    return match ? match[1] : null;
  };
  if (cmd === "visit") return 'Visited "' + truncateArg(arg("url") || "") + '"';
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
    rows.forEach((row) => {
      cancelAnim(row);
    });
    wrap.classList.remove("collapsed");
    const target = wrap.getBoundingClientRect().height;
    wrap._anim = wrap.animate(
      [
        { height: "0px", marginTop: -CHAT_GAP + "px" },
        { height: target + "px", marginTop: "0px" },
      ],
      { duration: 520, easing: EASE_OUT, fill: "backwards" },
    );
    const anim = wrap._anim;
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
    rows.forEach((row) => {
      cancelAnim(row);
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
      wrap._anim = wrap.animate(
        [
          { height: start + "px", marginTop: "0px" },
          { height: "0px", marginTop: -CHAT_GAP + "px" },
        ],
        { duration: 420, easing: EASE_IN, fill: "forwards" },
      );
      const anim = wrap._anim;
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
  Sfx.beginToolWait();
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
  if (replaying || !el.isConnected) return;
  el.classList.add("entering");
  if (REDUCE || !el.getBoundingClientRect().height) {
    /* No motion (reduced motion, or nothing to reveal): still render
       the element for real for one painted frame. Dropping .entering
       immediately would let content-visibility show the next frame at
       the contain-intrinsic-size placeholder, and the element would
       flash tall and snap to its real height. */
    requestAnimationFrame(() =>
      requestAnimationFrame(() => el.classList.remove("entering")),
    );
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
  (row._detailSections || []).forEach((section) => {
    detail.appendChild(detailSection(section));
  });
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

/* Pending ask_user question state. liveAsk tracks the question awaiting
   an answer (id matches its tool_call events); pendingAnswer marks the
   mode where the composer is restored and the user's next message
   resolves the question. */
let liveAsk = null;
let pendingAnswer = false;
let askPanel = null;
let inlineAskRow = null;
let replayAskId = null;
let replayAskArgs = null;

function setComposerVisible(visible) {
  const composerEl = document.querySelector(".composer");
  if (composerEl._visible === visible) return;
  if (visible && composerEl._visible === undefined) {
    composerEl._visible = true;
    return;
  }
  composerEl._visible = visible;
  cancelAnim(composerEl);
  if (REDUCE) {
    composerEl.style.display = visible ? "" : "none";
    return;
  }
  if (!visible) {
    const cs = getComputedStyle(composerEl);
    const box = boxOf(composerEl, cs);
    composerEl.style.overflow = "hidden";
    const anim = composerEl.animate([{ ...box }, { ...BOX_ZERO }], {
      duration: 320,
      easing: EASE_IN,
      fill: "forwards",
    });
    composerEl._anim = anim;
    anim.onfinish = () => {
      if (composerEl._visible === false) composerEl.style.display = "none";
      composerEl._anim = null;
      anim.cancel();
      composerEl.style.overflow = "";
    };
  } else {
    composerEl.style.display = "";
    const cs = getComputedStyle(composerEl);
    const box = boxOf(composerEl, cs);
    const anim = composerEl.animate([{ ...BOX_ZERO }, { ...box }], {
      duration: 320,
      easing: EASE_OUT,
    });
    composerEl._anim = anim;
    anim.onfinish = () => {
      composerEl._anim = null;
      anim.cancel();
    };
  }
}

function buildAskPanel(ask) {
  const panel = document.createElement("div");
  panel.className = "ask-panel glass";
  const header = document.createElement("div");
  header.className = "ask-header";
  const question = document.createElement("div");
  question.className = "ask-question";
  question.textContent = ask.question;
  const stop = document.createElement("button");
  stop.type = "button";
  stop.className = "icon-btn ask-stop";
  stop.title = "Stop";
  stop.setAttribute("aria-label", "Stop");
  stop.innerHTML = ICONS.stop;
  stop.addEventListener("click", () => {
    if (ws && ws.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({ type: "stop" }));
    if (window.Voice) Voice.stop();
    setStatus("working", "Stopping...");
    dismissAskPanel();
  });
  header.appendChild(question);
  header.appendChild(stop);
  panel.appendChild(header);
  const list = document.createElement("div");
  list.className = "ask-choices";
  ask.choices.forEach((choiceText) => {
    const choice = document.createElement("button");
    choice.type = "button";
    choice.className = "ask-choice";
    choice.textContent = choiceText;
    choice.addEventListener("click", () => {
      Sfx.play("send");
      if (ws && ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: "answer", choice: choiceText }));
      dismissAskPanel();
    });
    list.appendChild(choice);
  });
  const other = document.createElement("button");
  other.type = "button";
  other.className = "ask-choice other";
  other.textContent = "Other: Type your own response";
  other.addEventListener("click", () => {
    Sfx.play("click");
    dismissAskPanel();
    pendingAnswer = true;
    setSendState();
    input.focus();
  });
  list.appendChild(other);
  panel.appendChild(list);
  return panel;
}

function addInlineAsk(question) {
  inlineAskRow = document.createElement("div");
  inlineAskRow.className = "ask-inline glass";
  inlineAskRow.innerHTML = ICONS.help + '<span class="ask-inline-text"></span>';
  inlineAskRow.querySelector(".ask-inline-text").textContent = question;
  append(inlineAskRow);
}

const WAITING_LABEL = "Waiting for your answer...";

function showWaitingStatus() {
  setStatus("working", WAITING_LABEL);
}

function dismissAskPanel() {
  if (!askPanel) return;
  const panel = askPanel;
  askPanel = null;
  shrinkOut(panel);
  setComposerVisible(true);
}

function dockAsk(event) {
  const args = event.arguments || {};
  const question = String(args.question_text || "");
  const choices = Array.isArray(args.choices) ? args.choices.map(String) : [];
  liveAsk = { id: event.id, question, choices };
  setQueueSendDisabled(true);
  showWaitingStatus();
  if (!choices.length) {
    pendingAnswer = true;
    addInlineAsk(question);
    setSendState();
    return;
  }
  setComposerVisible(false);
  askPanel = buildAskPanel(liveAsk);
  const footerEl = document.querySelector("footer");
  footerEl.insertBefore(askPanel, footerEl.querySelector(".composer"));
  growIn(askPanel, 140);
  setSendState();
}

function undockAsk() {
  if (!liveAsk) return;
  liveAsk = null;
  setQueueSendDisabled(false);
  pendingAnswer = false;
  if (inlineAskRow) {
    const row = inlineAskRow;
    inlineAskRow = null;
    shrinkOut(row);
  }
  dismissAskPanel();
  if (statusLabel.textContent === WAITING_LABEL) {
    setStatus("working", "Thinking...");
  }
  setSendState();
}

function resetAskState() {
  liveAsk = null;
  pendingAnswer = false;
  replayAskId = null;
  replayAskArgs = null;
  if (askPanel) {
    askPanel.remove();
    askPanel = null;
  }
  if (inlineAskRow) {
    inlineAskRow.remove();
    inlineAskRow = null;
  }
  setComposerVisible(true);
  setSendState();
}

/* Queued messages. A message sent while the agent is busy never enters
   the chat: it waits as a pill docked right on top of the input field.
   "Send now" hands it to the server for delivery at the turn's next
   opening; the matching user_message event (same id) turns the pill
   into a chat bubble when the message actually reaches history. */
let queuedMessages = [];
let queueDock = null;

const QUEUE_PREVIEW_CHARS = 80;

function queuePreview(content) {
  const text = String(content || "")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > QUEUE_PREVIEW_CHARS
    ? text.slice(0, QUEUE_PREVIEW_CHARS) + "..."
    : text;
}

function addQueuedMessage(id, content) {
  queuedMessages.push({ id: id, content: content });
  if (!queueDock) {
    queueDock = document.createElement("div");
    queueDock.className = "queue-dock";
    const footerEl = document.querySelector("footer");
    footerEl.insertBefore(queueDock, footerEl.querySelector(".composer"));
    growIn(queueDock);
  }
  const pill = document.createElement("div");
  pill.className = "queue-pill glass";
  pill.dataset.queueId = id;
  const label = document.createElement("span");
  label.className = "queue-label";
  label.textContent = "Queued";
  const text = document.createElement("span");
  text.className = "queue-text";
  text.textContent = queuePreview(content);
  const sendNowBtn = document.createElement("button");
  sendNowBtn.type = "button";
  sendNowBtn.className = "queue-send icon-btn";
  sendNowBtn.title = "Send now";
  sendNowBtn.setAttribute("aria-label", "Send now");
  sendNowBtn.innerHTML = ICONS.send;
  if (liveAsk) {
    sendNowBtn.disabled = true;
    sendNowBtn.title = "Answer Syna's question first";
  }
  sendNowBtn.addEventListener("click", () => {
    sendNow(id, sendNowBtn);
  });
  pill.appendChild(label);
  pill.appendChild(text);
  pill.appendChild(sendNowBtn);
  queueDock.appendChild(pill);
  growIn(pill);
}

function sendNow(id, btn) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  Sfx.play("send");
  ws.send(JSON.stringify({ type: "send_now", id: id }));
  if (btn) btn.disabled = true;
}

/* While Syna waits for an answer, a "Send now" delivery can only happen
   after the question is resolved; disable the buttons so that shows. */
function setQueueSendDisabled(disabled) {
  if (!queueDock) return;
  queueDock.querySelectorAll(".queue-send").forEach((btn) => {
    btn.disabled = disabled;
    btn.title = disabled ? "Answer Syna's question first" : "Send now";
  });
}

function resolveQueuedMessage(id) {
  if (!queuedMessages.length) return;
  const index = queuedMessages.findIndex((item) => item.id === id);
  let removed;
  if (index >= 0) {
    removed = queuedMessages.splice(index, 1)[0];
  } else {
    console.warn(
      "Delivery for untracked queued message " +
        id +
        "; removing the oldest pill.",
    );
    removed = queuedMessages.shift();
  }
  const pill = queueDock
    ? queueDock.querySelector(
        '[data-queue-id="' + CSS.escape(removed.id) + '"]',
      )
    : null;
  if (pill) shrinkOut(pill);
  if (!queuedMessages.length && queueDock) {
    const dock = queueDock;
    queueDock = null;
    shrinkOut(dock);
  }
}

function clearQueuedMessages() {
  queuedMessages = [];
  if (queueDock) {
    queueDock.remove();
    queueDock = null;
  }
}

function finishToolRow(event) {
  let row = document.getElementById("tool-" + event.id);
  if (!row) {
    addToolRow(event);
    row = document.getElementById("tool-" + event.id);
  }
  row.classList.remove("running");
  if (runningToolRow === row) {
    runningToolRow = null;
    Sfx.endToolWait();
  }
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
    const backgroundRef = !event.failed && backgroundIdFor(event);
    if (backgroundRef) {
      row.addEventListener("click", () =>
        openBackgroundModal(backgroundRef.kind, backgroundRef.id),
      );
    } else {
      row.addEventListener("click", () => toggleDetail(row));
    }
  }
  updateAmbient();
}

/* The harness never lets the model see a result while a call is still
   running, so any tool row still marked running when the model speaks or
   the turn ends is stale; e.g. its finished event was delivered to
   another open window. Settle it so its animation cannot run forever. */
function finishStaleToolRows() {
  const stale = document.querySelectorAll(".tool-row.running");
  stale.forEach((row) => {
    row.classList.remove("running");
    if (runningToolRow === row) {
      runningToolRow = null;
      Sfx.endToolWait();
    }
  });
  if (stale.length) updateAmbient();
}

function addFileChip(event) {
  closeFileGroup();
  const chipHost = document.createElement("div");
  chipHost.className =
    "files-group open" + (event.type === "file_sent" ? " right" : "");
  const chip = document.createElement("a");
  chip.className =
    "file-chip glass" + (event.type === "file_sent" ? " outgoing" : "");
  chip.href = event.url ? withToken(API_BASE + event.url) : "#";
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
    event.type !== "compaction" &&
    event.type !== "subagent_message" &&
    event.type !== "background_done" &&
    event.type !== "rewind_state" &&
    event.type !== "history_rewind"
  ) {
    resetToolRun();
  }
  switch (event.type) {
    case "user_message":
      if (event.queued) {
        addQueuedMessage(event.id, event.content);
      } else {
        if (event.id) resolveQueuedMessage(event.id);
        addBubble(event.content, "user", event.id);
      }
      break;
    case "rewind_state":
      rewindAvailable = !!event.available;
      (event.added || []).forEach((id) => rewindIds.add(id));
      (event.removed || []).forEach((id) => rewindIds.delete(id));
      applyRewindState();
      break;
    case "history_rewind":
      rewindTranscript(event.id, event.content);
      break;
    case "model_message":
      finishStaleToolRows();
      if (!replaying && window.Voice && Voice.willNarrate(event.content)) {
        Voice.narrate(event.content, () => addBubble(event.content, "model"));
      } else {
        addBubble(event.content, "model");
        if (!replaying && window.Voice) Voice.replyDelivered();
      }
      break;
    case "history_done":
      replaying = false;
      if (replayAskId !== null) {
        dockAsk({ id: replayAskId, arguments: replayAskArgs || {} });
      }
      replayAskId = null;
      replayAskArgs = null;
      break;
    case "tool_call":
      if (event.name === "ask_user") {
        if (event.status === "started") {
          addToolRow(event);
          if (replaying) {
            replayAskId = event.id;
            replayAskArgs = event.arguments || null;
          } else {
            dockAsk(event);
          }
        } else {
          finishToolRow(event);
          if (replaying && event.id === replayAskId) {
            replayAskId = null;
            replayAskArgs = null;
          }
          undockAsk();
        }
        break;
      }
      if (event.status === "started") addToolRow(event);
      else finishToolRow(event);
      Sfx.poke();
      if (
        event.status !== "started" &&
        (event.name === "process" || event.name === "subagent")
      )
        refreshBackgroundList();
      break;
    case "subagent_message":
      if (
        bgModal &&
        bgModal.kind === "subagent" &&
        bgModal.id === event.subagent_id &&
        event.seq > bgModal.lastSeq
      ) {
        bgModal.lastSeq = event.seq;
        if (event.kind === "done") {
          setBgOutcome(bgEndLabel("subagent", { summary: event.summary }));
        } else {
          appendBgEntry(event);
        }
      }
      break;
    case "background_done":
      /* No chat row: the outcome lives in the work's tool row and the
         panel's viewer modal; the event just refreshes those. */
      refreshBackgroundList();
      if (bgModal && bgModal.id === event.id) {
        setBgOutcome(bgEndLabel(event.kind, { summary: event.summary }));
      }
      break;
    case "file":
    case "file_sent":
      addFileChip(event);
      break;
    case "compaction":
      addCompactionRow(event);
      break;
    case "info":
      addEventRow(event.text, false);
      break;
    case "status":
      if (!event.quiet) setStatus("working", event.text);
      break;
    case "busy":
      busy = true;
      setStatus("working", "Working...");
      setSendState();
      updateToolRunThinking();
      updateAmbient();
      break;
    case "ready":
      finishStaleToolRows();
      busy = false;
      setStatus("ready", "Ready");
      setSendState();
      updateToolRunThinking();
      updateAmbient();
      break;
    case "error":
      finishStaleToolRows();
      addEventRow(event.text, true);
      setStatus("error", "Error");
      updateAmbient();
      break;
  }
}

function connect() {
  ws = new WebSocket(withToken(WS_BASE + "/ws/" + clientId));

  ws.onopen = () => {
    reconnectDelay = 1000;
    banner.classList.remove("visible");
    chat.innerHTML = "";
    toolRun = null;
    replaying = true;
    replayQueue = [];
    replayFlushPending = false;
    runningToolRow = null;
    Sfx.endToolWait();
    openFileGroup = null;
    thinkingRow = null;
    resetAskState();
    clearQueuedMessages();
    rewindAvailable = false;
    rewindIds.clear();
    closeBackgroundModal();
    if (window.Voice) Voice.reset();
    setStatus("ready", "Ready");
    updateAmbient();
    refreshBackgroundList();
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
    resetAskState();
    clearQueuedMessages();
    setSendState();
    setStatus("", "Connecting...");
    updateAmbient();
    banner.classList.add("visible");
    scheduleReconnect();
  };

  ws.onerror = () => ws.close();
}

/* Probe the server before each reconnect. A 401 means this page holds a
   token from a previous server process (e.g. after Save and Restart);
   reconnecting can never succeed, so reload to pick up the fresh token. */
function scheduleReconnect() {
  setTimeout(async () => {
    try {
      const probe = await fetch(withToken(API_BASE + "/api/settings"), {
        headers: authHeaders(),
      });
      if (probe.status === 401) {
        window.location.reload();
        return;
      }
    } catch (err) {
      // The server is still down; fall through and retry the socket.
    }
    connect();
    reconnectDelay = Math.min(reconnectDelay * 2, 10000);
  }, reconnectDelay);
}

/* Poll until the restarted server answers, then reload so the page
   picks up the fresh auth token. Any response means it is listening. */
async function waitForRestartThenReload() {
  for (;;) {
    try {
      await fetch(withToken(API_BASE + "/api/settings"), {
        headers: authHeaders(),
      });
      window.location.reload();
      return;
    } catch (err) {
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
  }
}

function setSendState() {
  const mode = busy && !pendingAnswer ? "stop" : "send";
  if (sendBtn.dataset.mode !== mode) {
    sendBtn.dataset.mode = mode;
    sendBtn.classList.remove("pop");
    void sendBtn.offsetWidth;
    sendBtn.classList.add("pop");
    sendBtn.innerHTML = busy && !pendingAnswer ? ICONS.stop : ICONS.send;
    sendBtn.title = busy && !pendingAnswer ? "Stop" : "Send";
  }
  document.querySelector(".composer").classList.toggle("busy", busy);
  allegroBtn.disabled = busy;
  sendBtn.disabled = busy && !pendingAnswer ? false : !input.value.trim();
}

function sendMessage() {
  const content = input.value.trim();
  if (!content || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(
    JSON.stringify({
      type: "message",
      id: crypto.randomUUID(),
      content: content,
      allegro: allegro.on,
    }),
  );
  Sfx.play("send");
  input.value = "";
  fitInput();
  setSendState();
  input.focus();
}

function sendStop() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "stop" }));
  if (window.Voice) Voice.stop();
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
      if (window.Voice) Voice.stop();
    }
    const response = await fetch(API_BASE + "/api/new-chat/" + clientId, {
      method: "POST",
      headers: authHeaders(),
    });
    if (!response.ok) throw new Error("HTTP " + response.status);
    if (bgBtn) bgBtn.hidden = true;
    if (ws) {
      ws.onclose = null;
      ws.close();
      ws = null;
    }
    chat.innerHTML = "";
    toolRun = null;
    replayQueue = [];
    runningToolRow = null;
    Sfx.endToolWait();
    openFileGroup = null;
    thinkingRow = null;
    resetAskState();
    clearQueuedMessages();
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
    fetch(API_BASE + "/api/upload/" + clientId, {
      method: "POST",
      body: form,
      headers: authHeaders(),
    })
      .then((response) => {
        if (!response.ok)
          throw new Error("Upload failed (" + response.status + ")");
      })
      .catch((err) =>
        addEventRow("Could not send " + file.name + ": " + err.message, true),
      );
  }
}

const SPINNER_SVG =
  '<svg class="btn-spinner" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10"/></svg>';

/* Background work modals: the process viewer (polled output) and the
   subagent viewer (streamed message rectangles) share one modal shell.
   Tool rows for process/subagent/wait_for open these instead of the
   inline detail. */
let bgModal = null;

const BG_TRIM_CHARS = 160000;

function backgroundIdFor(event) {
  // The dispatch tools nest their real arguments one level down
  // (`{action, args}`), so a targeted call's name lives in `args.args`.
  const args = event.arguments || null;
  const nested =
    args && args.args && typeof args.args === "object" ? args.args : {};
  const spawned = event.structured_output;
  const spawnedName = spawned && spawned.name ? String(spawned.name) : null;
  const targetedName =
    (nested.name ? String(nested.name) : null) ||
    (args && args.name != null ? String(args.name) : null);
  if (event.name === "process") {
    const id = spawnedName || targetedName;
    return id ? { kind: "process", id } : null;
  }
  if (event.name === "subagent") {
    const id = spawnedName || targetedName;
    return id ? { kind: "subagent", id } : null;
  }
  if (event.name === "wait_for") {
    const id = args && args.name;
    return id ? { kind: "either", id } : null;
  }
  return null;
}

function b64ToText(b64) {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function sendBgStop(_kind, id, btn) {
  if (!ws || ws.readyState !== WebSocket.OPEN || !bgModal) return;
  ws.send(
    JSON.stringify({
      type: bgModal.kind === "process" ? "process_kill" : "subagent_kill",
      id,
    }),
  );
  if (btn) btn.disabled = true;
  setBgState("stopping...");
}

function setBgState(state, exitCode, summary) {
  if (!bgModal) return;
  const chip = bgModal.stateChip;
  const stopBtn = bgModal.stopBtn;
  if (state === "running") {
    chip.hidden = false;
    chip.classList.add("running");
    chip.classList.remove("done");
    chip.textContent = "running";
    stopBtn.disabled = false;
    stopBtn.classList.remove("ended");
    stopBtn.textContent = "Stop";
    return;
  }
  if (state === "stopping...") {
    chip.hidden = false;
    chip.classList.remove("running", "done");
    chip.textContent = "stopping…";
    stopBtn.disabled = true;
    stopBtn.classList.remove("ended");
    stopBtn.textContent = "Stopping…";
    return;
  }
  if (state === "gone" || state === "unavailable") {
    setBgOutcome(state === "gone" ? "Gone" : "Unavailable");
    return;
  }
  setBgOutcome(
    bgEndLabel(bgModal.kind, { state, exit_code: exitCode, summary }),
  );
}

/* Once the work ends, the footer button stops being a button: disabled,
   outcome-styled, and carrying the short termination label. The header
   chip hides, so the outcome appears exactly once. */
function setBgOutcome(label) {
  if (!bgModal) return;
  bgModal.stateChip.hidden = true;
  const stopBtn = bgModal.stopBtn;
  stopBtn.disabled = true;
  stopBtn.classList.add("ended");
  stopBtn.textContent = label;
}

function appendBgProcessText(section, text) {
  if (!bgModal) return;
  let box = bgModal.sections[section];
  if (!box) {
    if (bgModal.emptyNote) {
      bgModal.emptyNote.remove();
      bgModal.emptyNote = null;
    }
    const label = document.createElement("div");
    label.className = "bg-output-label";
    label.textContent = section;
    box = document.createElement("pre");
    box.className = "bg-output-pre" + (section === "stderr" ? " stderr" : "");
    box._content = "";
    bgModal.sections[section] = box;
    bgModal.body.appendChild(label);
    bgModal.body.appendChild(box);
  }
  const body = bgModal.body;
  const nearBottom =
    body.scrollHeight - body.scrollTop - body.clientHeight < 60;
  box._content += text;
  if (box._content.length > BG_TRIM_CHARS)
    box._content = "[... trimmed ...]\n" + box._content.slice(-BG_TRIM_CHARS);
  box.textContent = box._content;
  if (nearBottom) body.scrollTop = body.scrollHeight;
}

function stopBgTimer() {
  if (bgModal && bgModal.timer) {
    clearInterval(bgModal.timer);
    bgModal.timer = null;
  }
}

function startProcessPolling(id) {
  const token = bgModal;
  let stdoutOffset = 0,
    stderrOffset = 0;
  /* Until output arrives the body would be a blank box; the note is
     removed by the first appendBgProcessText call. */
  const emptyNote = document.createElement("div");
  emptyNote.className = "bg-empty-note";
  emptyNote.textContent = "No output yet.";
  token.body.appendChild(emptyNote);
  token.emptyNote = emptyNote;
  const tick = async () => {
    if (bgModal !== token) return;
    let data = null,
      gone = false;
    try {
      const res = await fetch(
        withToken(
          `${API_BASE}/api/background/${clientId}/process/${encodeURIComponent(id)}/output?stdout_offset=${stdoutOffset}&stderr_offset=${stderrOffset}`,
        ),
        { headers: authHeaders() },
      );
      if (res.status === 404) gone = true;
      else if (res.ok) data = await res.json();
    } catch (err) {
      /* transient: retry on the next tick */
    }
    if (bgModal !== token) return;
    if (gone) {
      setBgState("gone");
      stopBgTimer();
      return;
    }
    if (!data) return;
    if (data.stdout_b64)
      appendBgProcessText("stdout", b64ToText(data.stdout_b64));
    if (data.stderr_b64)
      appendBgProcessText("stderr", b64ToText(data.stderr_b64));
    stdoutOffset = data.stdout_size;
    stderrOffset = data.stderr_size;
    if (data.state !== "running") {
      setBgOutcome(bgEndLabel("process", data));
      stopBgTimer();
    } else {
      setBgState("running");
    }
  };
  tick();
  token.timer = setInterval(tick, 1000);
}

function appendBgEntry(entry) {
  if (!bgModal) return;
  const el = document.createElement("div");
  if (entry.kind === "model") {
    el.className = "bg-msg model";
    el.textContent = entry.content || "";
  } else if (entry.kind === "tool") {
    el.className = "bg-msg tool" + (entry.failed ? " failed" : "");
    const name = document.createElement("span");
    name.className = "bg-tool-name";
    name.textContent =
      (entry.name || "tool") + (entry.status === "finished" ? "" : " ...");
    el.appendChild(name);
    const out = entry.output_preview || entry.signature || "";
    if (out) {
      const outEl = document.createElement("div");
      outEl.className = "bg-tool-out";
      outEl.textContent = out;
      el.appendChild(outEl);
    }
  } else if (entry.kind === "error") {
    el.className = "bg-msg error";
    el.textContent = entry.text || "";
  } else if (entry.kind === "notice") {
    el.className = "bg-msg notice";
    el.textContent = entry.text || "";
  } else {
    return;
  }
  const body = bgModal.body;
  const nearBottom =
    body.scrollHeight - body.scrollTop - body.clientHeight < 60;
  body.appendChild(el);
  if (nearBottom) body.scrollTop = body.scrollHeight;
}

function renderSubagentHistory(token, data) {
  if (bgModal !== token) return;
  const goal = document.createElement("div");
  goal.className = "bg-msg goal";
  goal.textContent = data.goal;
  token.body.appendChild(goal);
  for (const entry of data.events) appendBgEntry(entry);
  token.lastSeq = data.events.length
    ? data.events[data.events.length - 1].seq
    : 0;
  const stillRunning = data.state === "running" || data.state === "stopping";
  if (stillRunning) setBgState("running");
  else setBgOutcome(bgEndLabel("subagent", data));
  token.body.scrollTop = token.body.scrollHeight;
}

async function openSubagentView(id) {
  const token = bgModal;
  let data = null;
  try {
    const res = await fetch(
      withToken(
        `${API_BASE}/api/background/${clientId}/subagent/${encodeURIComponent(id)}/events`,
      ),
      { headers: authHeaders() },
    );
    if (res.ok) data = await res.json();
  } catch (err) {
    /* fall through to the unavailable state */
  }
  if (bgModal !== token) return;
  if (!data) {
    setBgState("unavailable");
    return;
  }
  renderSubagentHistory(token, data);
}

/* A wait_for row does not know which kind it waited on: the subagent
   endpoint decides (a process id is not registered there). */
async function resolveEitherView(id) {
  const token = bgModal;
  try {
    const res = await fetch(
      withToken(
        `${API_BASE}/api/background/${clientId}/subagent/${encodeURIComponent(id)}/events`,
      ),
      { headers: authHeaders() },
    );
    if (bgModal !== token) return;
    if (res.ok) {
      const data = await res.json();
      token.kind = "subagent";
      token.iconEl.textContent = "account_tree";
      renderSubagentHistory(token, data);
      return;
    }
  } catch (err) {
    /* not a subagent; try the process viewer */
  }
  if (bgModal !== token) return;
  token.kind = "process";
  token.iconEl.textContent = "terminal";
  startProcessPolling(id);
}

function openBackgroundModal(kind, id) {
  closeBackgroundModal();
  const backdrop = document.createElement("div");
  backdrop.className = "bg-modal-backdrop";
  const modal = document.createElement("div");
  modal.className = "bg-modal";
  const header = document.createElement("div");
  header.className = "bg-modal-header";
  const title = document.createElement("div");
  title.className = "bg-modal-title";
  const iconEl = document.createElement("span");
  iconEl.className = "material-symbols-outlined";
  const idEl = document.createElement("span");
  idEl.className = "bg-modal-id";
  const stateChip = document.createElement("span");
  stateChip.className = "bg-modal-state";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "icon-btn";
  closeBtn.title = "Close";
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.innerHTML = ICONS.close;
  const body = document.createElement("div");
  body.className = "bg-modal-body";
  const footer = document.createElement("div");
  footer.className = "bg-modal-footer";
  const stopBtn = document.createElement("button");
  stopBtn.type = "button";
  stopBtn.className = "bg-modal-stop";
  stopBtn.textContent = "Stop";
  title.append(iconEl, idEl);
  header.append(title, stateChip, closeBtn);
  footer.append(stopBtn);
  modal.append(header, body, footer);
  backdrop.appendChild(modal);
  document.body.appendChild(backdrop);
  requestAnimationFrame(() => backdrop.classList.add("open"));
  bgModal = {
    kind,
    id,
    backdrop,
    body,
    stateChip,
    stopBtn,
    iconEl,
    timer: null,
    lastSeq: 0,
    sections: {},
  };
  idEl.textContent = id;
  iconEl.textContent = kind === "process" ? "terminal" : "account_tree";
  closeBtn.addEventListener("click", closeBackgroundModal);
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) closeBackgroundModal();
  });
  stopBtn.addEventListener("click", () => sendBgStop(kind, id, stopBtn));
  Sfx.play("open");
  if (kind === "process") startProcessPolling(id);
  else if (kind === "subagent") openSubagentView(id);
  else resolveEitherView(id);
}

function closeBackgroundModal() {
  if (!bgModal) return;
  const backdrop = bgModal.backdrop;
  stopBgTimer();
  bgModal = null;
  backdrop.classList.remove("open");
  setTimeout(() => backdrop.remove(), REDUCE ? 0 : 250);
  Sfx.play("close");
}

/* Top-bar indicator for running background work. A small poll keeps the
   button honest even when a spawn or completion event is missed; the
   explicit refreshes after those events keep it instant. */
let bgListTimer = null;
let bgListData = { processes: [], subagents: [] };

async function refreshBackgroundList() {
  try {
    const res = await fetch(
      withToken(`${API_BASE}/api/background/${clientId}/list`),
      { headers: authHeaders() },
    );
    bgListData = res.ok ? await res.json() : { processes: [], subagents: [] };
  } catch (err) {
    bgListData = { processes: [], subagents: [] };
  }
  /* The button appears with the chat's first spawn and then stays for
     the chat's lifetime: the registry never releases records, so any
     row at all means work was spawned. Only a new chat (registry torn
     down -> 404/empty) or closing the app clears it. */
  const hasWork =
    (bgListData.processes && bgListData.processes.length > 0) ||
    (bgListData.subagents && bgListData.subagents.length > 0);
  if (bgBtn) bgBtn.hidden = !hasWork;
  return bgListData;
}

function startBgListPolling() {
  if (bgListTimer) return;
  refreshBackgroundList();
  bgListTimer = setInterval(refreshBackgroundList, 4000);
}

let bgListModal = null;

function closeBackgroundListModal() {
  if (!bgListModal) return;
  const backdrop = bgListModal.backdrop;
  if (bgListModal.timer) {
    clearInterval(bgListModal.timer);
    bgListModal.timer = null;
  }
  bgListModal = null;
  backdrop.classList.remove("open");
  setTimeout(() => backdrop.remove(), REDUCE ? 0 : 250);
  Sfx.play("close");
}

function bgListCard(row, kind) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = "bglist-card";
  const icon = document.createElement("span");
  icon.className = "material-symbols-outlined";
  icon.textContent = kind === "process" ? "terminal" : "account_tree";
  const main = document.createElement("span");
  main.className = "bglist-card-main";
  const name = document.createElement("span");
  name.className = "bglist-card-name";
  name.textContent = row.name;
  const meta = document.createElement("span");
  meta.className = "bglist-card-meta";
  meta.textContent = (
    kind === "process" ? row.command || "" : row.goal || ""
  ).slice(0, 140);
  main.append(name, meta);
  const chip = document.createElement("span");
  chip.className =
    "bglist-card-state" + (row.state === "running" ? " running" : "");
  chip.textContent =
    bgListStateLabel(kind, row) + " · " + bgRuntimeLabel(row.started);
  card.append(icon, main, chip);
  card.addEventListener("click", () => {
    closeBackgroundListModal();
    openBackgroundModal(kind, row.name);
  });
  return card;
}

function renderBgListTab() {
  if (!bgListModal) return;
  const kind = bgListModal.kind;
  const list = bgListModal.list;
  list.textContent = "";
  const rows = bgListData[kind === "process" ? "processes" : "subagents"] || [];
  const running = rows.filter((row) => row.state === "running");
  const finished = rows.filter((row) => row.state !== "running");
  if (running.length)
    for (const row of running) list.appendChild(bgListCard(row, kind));
  if (running.length && finished.length) {
    const separator = document.createElement("div");
    separator.className = "bglist-separator";
    list.appendChild(separator);
  }
  for (const row of finished) list.appendChild(bgListCard(row, kind));
  if (!running.length && !finished.length) {
    const empty = document.createElement("div");
    empty.className = "bglist-empty";
    empty.textContent =
      kind === "process" ? "No processes yet." : "No subagents yet.";
    list.appendChild(empty);
  }
}

function openBackgroundListModal() {
  if (bgListModal) return;
  closeBackgroundModal();
  const backdrop = document.createElement("div");
  backdrop.className = "bg-modal-backdrop";
  const modal = document.createElement("div");
  modal.className = "bg-modal bglist-modal";
  const header = document.createElement("div");
  header.className = "bg-modal-header";
  const title = document.createElement("div");
  title.className = "bg-modal-title";
  const iconEl = document.createElement("span");
  iconEl.className = "material-symbols-outlined";
  iconEl.textContent = "list_alt";
  const titleText = document.createElement("span");
  titleText.className = "bg-modal-id";
  titleText.textContent = "Background work";
  title.append(iconEl, titleText);
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "icon-btn";
  closeBtn.title = "Close";
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.innerHTML = ICONS.close;
  header.append(title, closeBtn);
  const tabs = document.createElement("div");
  tabs.className = "bglist-tabs";
  const list = document.createElement("div");
  list.className = "bglist-list";
  modal.append(header, tabs, list);
  backdrop.appendChild(modal);
  document.body.appendChild(backdrop);
  requestAnimationFrame(() => backdrop.classList.add("open"));
  bgListModal = { backdrop, tabs, list, kind: "subagent", timer: null };
  for (const kind of ["subagent", "process"]) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "bglist-tab";
    tab.textContent = kind === "process" ? "Processes" : "Subagents";
    tab.dataset.kind = kind;
    tab.addEventListener("click", () => {
      bgListModal.kind = kind;
      paintBgListTabs();
      renderBgListTab();
    });
    tabs.appendChild(tab);
  }
  function paintBgListTabs() {
    for (const tab of tabs.children)
      tab.classList.toggle("active", tab.dataset.kind === bgListModal.kind);
  }
  closeBtn.addEventListener("click", closeBackgroundListModal);
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) closeBackgroundListModal();
  });
  const refresh = async () => {
    await refreshBackgroundList();
    if (bgListModal) renderBgListTab();
  };
  paintBgListTabs();
  refresh();
  bgListModal.timer = setInterval(refresh, 2000);
  Sfx.play("open");
}

async function openSettingsModal() {
  const existing = document.querySelector(".settings-modal-backdrop");
  if (existing) {
    existing.classList.remove("open");
    setTimeout(() => existing.remove(), 320);
    return;
  }

  let data;
  try {
    const res = await fetch(withToken(API_BASE + "/api/settings"), {
      headers: authHeaders(),
    });
    if (!res.ok)
      throw new Error("Could not load settings (" + res.status + ")");
    data = await res.json();
  } catch (err) {
    alert("Could not load settings: " + err.message);
    return;
  }

  const env = { ...(data.env || {}) };
  const config = { ...(data.config || {}) };
  const defaults = { ...(data.defaults || {}) };
  const providers = data.providers || {};

  const initialSnapshot = JSON.stringify({ env, config });

  const backdrop = document.createElement("div");
  backdrop.className = "settings-modal-backdrop";

  let closing = false;
  function closeSettingsModal() {
    if (closing) return;
    closing = true;
    backdrop.classList.remove("open");
    setTimeout(() => backdrop.remove(), 320);
  }

  const modal = document.createElement("div");
  modal.className = "settings-modal";

  // Header
  const header = document.createElement("div");
  header.className = "settings-modal-header";
  header.innerHTML = `
    <h3 class="settings-modal-title">${ICONS.settings}<span>Settings</span></h3>
    <button class="icon-btn settings-close-btn" title="Close" aria-label="Close">${ICONS.close}</button>
  `;
  const closeBtn = header.querySelector(".settings-close-btn");
  closeBtn.addEventListener("click", closeSettingsModal);
  modal.appendChild(header);

  // Body
  const body = document.createElement("div");
  body.className = "settings-modal-body";

  const dirtyCheck = () => {
    const currentSnapshot = JSON.stringify({ env, config });
    saveBtn.disabled = currentSnapshot === initialSnapshot;
  };

  const createField = ({
    label,
    desc,
    value,
    defaultValue,
    isSelect,
    options,
    type = "text",
    placeholder = "",
    allowRestore = true,
    onChange,
  }) => {
    const row = document.createElement("div");
    row.className = "settings-row";

    const rowHeader = document.createElement("div");
    rowHeader.className = "settings-row-header";

    const labelEl = document.createElement("label");
    labelEl.className = "settings-label";
    labelEl.textContent = label;
    if (desc) labelEl.title = desc;
    rowHeader.appendChild(labelEl);

    let control;
    const hasDefault = defaultValue !== undefined && defaultValue !== "";

    if (allowRestore && (isSelect || hasDefault)) {
      const resetBtn = document.createElement("button");
      resetBtn.className = "settings-reset-btn";
      resetBtn.title = isSelect ? "Select the default option" : "Clear input";
      resetBtn.setAttribute("aria-label", "Reset to default");
      resetBtn.innerHTML = ICONS.reset;
      resetBtn.addEventListener("click", () => {
        if (isSelect) {
          control.selectedIndex = 0;
        } else {
          control.value = "";
        }
        control.dispatchEvent(new Event(isSelect ? "change" : "input"));
        control.focus();
      });
      rowHeader.appendChild(resetBtn);
    }
    row.appendChild(rowHeader);

    if (isSelect) {
      control = document.createElement("select");
      control.className = "settings-select";
      if (desc) control.title = desc;
      for (const opt of options) {
        const optEl = document.createElement("option");
        optEl.value = opt.value;
        optEl.textContent = opt.label;
        control.appendChild(optEl);
      }
      control.value = value;
      control.addEventListener("change", (e) => {
        onChange(e.target.value);
        dirtyCheck();
      });
      row.appendChild(control);
    } else {
      control = document.createElement("input");
      control.className = "settings-input";
      control.type = type;
      control.value = value ?? "";
      if (hasDefault) control.placeholder = `${defaultValue} (Default)`;
      else if (placeholder) control.placeholder = placeholder;
      if (desc) control.title = desc;
      control.addEventListener("input", (e) => {
        onChange(e.target.value);
        dirtyCheck();
      });
      row.appendChild(control);
    }

    return { row, control };
  };

  // Right-aligned on/off switch bound to a config key; editing the
  // category's fields stays possible either way.
  const categoryToggle = (key, fallback, title) => {
    const wrap = document.createElement("label");
    wrap.className = "settings-toggle";
    wrap.title = title;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = config[key] === undefined ? fallback : Boolean(config[key]);
    input.addEventListener("change", () => {
      config[key] = input.checked;
      dirtyCheck();
    });
    wrap.appendChild(input);
    const text = document.createElement("span");
    text.textContent = "Enabled";
    wrap.appendChild(text);
    return wrap;
  };

  const categoryHeader = (title, toggle) => {
    const header = document.createElement("div");
    header.className = "settings-category-header";
    header.innerHTML = `<h4 class="settings-category-title">${title}</h4>`;
    if (toggle) header.appendChild(toggle);
    return header;
  };

  // --- Category 1: Provider and API Key ---
  const cat1 = document.createElement("div");
  cat1.className = "settings-group";
  cat1.innerHTML =
    '<h4 class="settings-category-title">Provider and API Key</h4>';

  const providerOpts = Object.entries(providers).map(([value, preset]) => ({
    value,
    label: preset.label || preset.name,
  }));
  providerOpts.push({ value: "custom", label: "Custom endpoint" });
  const { row: providerRow } = createField({
    label: "Provider",
    desc: "Target LLM service provider.",
    value: env.provider || "openrouter",
    isSelect: true,
    options: providerOpts,
    allowRestore: false,
    onChange: (val) => {
      env.provider = val;
      const preset = providers[val];
      if (preset) {
        if (preset.base_url) env.base_url = preset.base_url;
        if (preset.model) env.model = preset.model;
      }
      urlFieldWrapper.style.display = val === "custom" ? "" : "none";
      keyFieldWrapper.style.display = val === "ollama" ? "none" : "";
    },
  });
  cat1.appendChild(providerRow);

  const { row: keyRow } = createField({
    label: "API Key",
    desc: "Your secret API key for the chosen LLM provider.",
    value: env.api_key || "",
    type: "password",
    allowRestore: false,
    onChange: (val) => {
      env.api_key = val;
    },
  });
  const keyFieldWrapper = keyRow;
  if (env.provider === "ollama") keyFieldWrapper.style.display = "none";
  cat1.appendChild(keyFieldWrapper);

  const { row: urlRow } = createField({
    label: "Endpoint URL",
    desc: "Base URL for the provider API.",
    value: env.base_url || "",
    defaultValue: defaults.base_url,
    onChange: (val) => {
      env.base_url = val;
    },
  });
  const urlFieldWrapper = urlRow;
  if (env.provider !== "custom") urlFieldWrapper.style.display = "none";
  cat1.appendChild(urlFieldWrapper);

  body.appendChild(cat1);

  // --- Category 2: Text & Reasoning ---
  const cat2 = document.createElement("div");
  cat2.className = "settings-group";
  cat2.innerHTML = '<h4 class="settings-category-title">Text & Reasoning</h4>';

  const { row: modelRow } = createField({
    label: "Main Model",
    desc: "The primary agent model driven by Syna.",
    value: env.model || "",
    defaultValue: defaults.model,
    onChange: (val) => {
      env.model = val;
    },
  });
  cat2.appendChild(modelRow);

  const effortOpts = [
    { value: "medium", label: "medium (Default)" },
    { value: "none", label: "none" },
    { value: "minimal", label: "minimal" },
    { value: "low", label: "low" },
    { value: "high", label: "high" },
    { value: "xhigh", label: "xhigh" },
    { value: "max", label: "max" },
  ];
  const { row: effortRow } = createField({
    label: "Thinking Effort",
    desc: "Reasoning budget/effort for reasoning models.",
    value: env.thinking_effort || defaults.thinking_effort,
    defaultValue: defaults.thinking_effort,
    isSelect: true,
    options: effortOpts,
    onChange: (val) => {
      env.thinking_effort = val;
    },
  });
  cat2.appendChild(effortRow);

  const { row: verifierModelRow } = createField({
    label: "Verifier Model",
    desc: "Optional reviewer model for the review_work tool (defaults to main model when unset).",
    value: env.verifier_model || "",
    defaultValue: "",
    placeholder: "Same as main model (Default)",
    onChange: (val) => {
      env.verifier_model = val;
    },
  });
  cat2.appendChild(verifierModelRow);

  const verifierEffortOpts = [
    { value: "high", label: "high (Default)" },
    { value: "none", label: "none" },
    { value: "minimal", label: "minimal" },
    { value: "low", label: "low" },
    { value: "medium", label: "medium" },
    { value: "xhigh", label: "xhigh" },
    { value: "max", label: "max" },
  ];
  const { row: verifierEffortRow } = createField({
    label: "Verifier Effort",
    desc: "Reasoning effort for the review_work reviewer request.",
    value: env.verifier_model_effort || defaults.verifier_model_effort,
    defaultValue: defaults.verifier_model_effort,
    isSelect: true,
    options: verifierEffortOpts,
    onChange: (val) => {
      env.verifier_model_effort = val;
    },
  });
  cat2.appendChild(verifierEffortRow);

  body.appendChild(cat2);

  // --- Category 3: Voice ---
  const cat3 = document.createElement("div");
  cat3.className = "settings-group";
  cat3.appendChild(
    categoryHeader(
      "Voice",
      categoryToggle(
        "voice_enabled",
        true,
        "Allow voice dictation and response read-aloud",
      ),
    ),
  );

  const { row: sttRow } = createField({
    label: "Speech-to-Text Model (STT)",
    desc: "Model used for transcribing microphone voice recordings.",
    value: env.stt_model || "",
    defaultValue: defaults.stt_model,
    onChange: (val) => {
      env.stt_model = val;
    },
  });
  cat3.appendChild(sttRow);

  const { row: ttsRow } = createField({
    label: "Text-to-Speech Model (TTS)",
    desc: "Model used for speaking agent responses aloud.",
    value: env.tts_model || "",
    defaultValue: defaults.tts_model,
    onChange: (val) => {
      env.tts_model = val;
    },
  });
  cat3.appendChild(ttsRow);

  const voiceOpts = [
    { value: "Kore", label: "Kore (Default)" },
    { value: "Alloy", label: "Alloy" },
    { value: "Echo", label: "Echo" },
    { value: "Fable", label: "Fable" },
    { value: "Onyx", label: "Onyx" },
    { value: "Nova", label: "Nova" },
    { value: "Shimmer", label: "Shimmer" },
  ];
  const { row: voiceRow } = createField({
    label: "TTS Voice",
    desc: "Voice persona for speech synthesis.",
    value: env.tts_voice || defaults.tts_voice,
    defaultValue: defaults.tts_voice,
    isSelect: true,
    options: voiceOpts,
    onChange: (val) => {
      env.tts_voice = val;
    },
  });
  cat3.appendChild(voiceRow);

  const langOpts = [
    { value: "en", label: "en - English (Default)" },
    { value: "es", label: "es - Spanish" },
    { value: "fr", label: "fr - French" },
    { value: "de", label: "de - German" },
    { value: "it", label: "it - Italian" },
    { value: "pt", label: "pt - Portuguese" },
    { value: "zh", label: "zh - Chinese" },
    { value: "ja", label: "ja - Japanese" },
    { value: "ko", label: "ko - Korean" },
    { value: "auto", label: "auto - Detect Automatically" },
  ];
  const { row: langRow } = createField({
    label: "Spoken Language",
    desc: "Spoken language hint for voice transcription.",
    value: env.speech_language || defaults.speech_language,
    defaultValue: defaults.speech_language,
    isSelect: true,
    options: langOpts,
    onChange: (val) => {
      env.speech_language = val;
    },
  });
  cat3.appendChild(langRow);

  const continuousOpts = [
    { value: "off", label: "Off (Default)" },
    { value: "on", label: "On" },
  ];
  const { row: continuousRow } = createField({
    label: "Continuous conversation",
    desc: "In voice mode, automatically start listening after each reply.",
    value: config.continuous_conversation ? "on" : "off",
    defaultValue: "off",
    isSelect: true,
    options: continuousOpts,
    onChange: (val) => {
      config.continuous_conversation = val === "on";
    },
  });
  cat3.appendChild(continuousRow);

  body.appendChild(cat3);

  // --- Category 4: Images ---
  const cat4 = document.createElement("div");
  cat4.className = "settings-group";
  cat4.appendChild(
    categoryHeader(
      "Images",
      categoryToggle(
        "image_generation_enabled",
        false,
        "Allow the model to generate images",
      ),
    ),
  );

  const { row: imgRow } = createField({
    label: "Image Generation Model",
    desc: "Model used by the text_to_image tool to generate images.",
    value: env.image_model || "",
    defaultValue: defaults.image_model,
    onChange: (val) => {
      env.image_model = val;
    },
  });
  cat4.appendChild(imgRow);

  body.appendChild(cat4);

  // --- Checkpoints ---
  const catCheck = document.createElement("div");
  catCheck.className = "settings-group";
  catCheck.appendChild(categoryHeader("Workspace"));

  const checkOpts = [
    { value: "off", label: "No (Default)" },
    { value: "on", label: "Yes (checkpoints carry some delay)" },
  ];
  const { row: checkRow } = createField({
    label: "Enable checkpoints",
    desc: "Snapshot the workspace before each message so it can be rewound to.",
    value: config.checkpoints_enabled ? "on" : "off",
    defaultValue: "off",
    isSelect: true,
    options: checkOpts,
    onChange: (val) => {
      config.checkpoints_enabled = val === "on";
    },
  });
  catCheck.appendChild(checkRow);

  body.appendChild(catCheck);

  // --- Advanced (collapsed by default) ---
  const cat5 = document.createElement("details");
  cat5.className = "settings-details";
  const cat5Summary = document.createElement("summary");
  cat5Summary.textContent = "Advanced Runtime Settings";
  cat5.appendChild(cat5Summary);

  const cat5Body = document.createElement("div");
  cat5Body.className = "settings-group";
  cat5Body.style.marginTop = "14px";

  const { row: stepsRow } = createField({
    label: "Max Steps Per Turn",
    desc: "Maximum model requests allowed per turn.",
    value: config.max_steps_per_turn,
    defaultValue: defaults.max_steps_per_turn,
    type: "number",
    onChange: (val) => {
      config.max_steps_per_turn = Number(val) || defaults.max_steps_per_turn;
    },
  });
  cat5Body.appendChild(stepsRow);

  const { row: cmdTimeoutRow } = createField({
    label: "Command Timeout (seconds)",
    desc: "Timeout in seconds for execute_command inside the pod.",
    value: config.command_timeout_s,
    defaultValue: defaults.command_timeout_s,
    type: "number",
    onChange: (val) => {
      config.command_timeout_s = Number(val) || defaults.command_timeout_s;
    },
  });
  cat5Body.appendChild(cmdTimeoutRow);

  const { row: toolTimeoutRow } = createField({
    label: "Tool Call Timeout (seconds)",
    desc: "Timeout in seconds for individual tool calls before detaching.",
    value: config.tool_call_timeout_s,
    defaultValue: defaults.tool_call_timeout_s,
    type: "number",
    onChange: (val) => {
      config.tool_call_timeout_s = Number(val) || defaults.tool_call_timeout_s;
    },
  });
  cat5Body.appendChild(toolTimeoutRow);

  const { row: memRow } = createField({
    label: "Pod Memory Limit (MB)",
    desc: "Pod memory ceiling in MB guarding the host against runaway processes.",
    value: config.pod_memory_mb,
    defaultValue: defaults.pod_memory_mb,
    type: "number",
    onChange: (val) => {
      config.pod_memory_mb = Number(val) || defaults.pod_memory_mb;
    },
  });
  cat5Body.appendChild(memRow);

  const { row: ctxBudgetRow } = createField({
    label: "Context Budget (tokens)",
    desc: "Estimated-token ceiling for the entire conversation.",
    value: config.context_budget_tokens,
    defaultValue: defaults.context_budget_tokens,
    type: "number",
    onChange: (val) => {
      config.context_budget_tokens =
        Number(val) || defaults.context_budget_tokens;
    },
  });
  cat5Body.appendChild(ctxBudgetRow);

  const { row: outLimitRow } = createField({
    label: "Output Limit (chars)",
    desc: "Tool output character ceiling above which output is cut to head and tail.",
    value: config.max_command_output_chars,
    defaultValue: defaults.max_command_output_chars,
    type: "number",
    onChange: (val) => {
      config.max_command_output_chars =
        Number(val) || defaults.max_command_output_chars;
    },
  });
  cat5Body.appendChild(outLimitRow);

  cat5.appendChild(cat5Body);
  body.appendChild(cat5);

  // --- Allegro mode note (read-only) ---
  const cat6 = document.createElement("details");
  cat6.className = "settings-details";
  const cat6Summary = document.createElement("summary");
  cat6Summary.textContent = "Allegro Mode";
  cat6.appendChild(cat6Summary);

  const cat6Body = document.createElement("div");
  cat6Body.className = "settings-group";
  cat6Body.style.marginTop = "14px";

  const allegroNote = document.createElement("div");
  allegroNote.className = "settings-note";
  const numberOf = (value, fallback) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  };
  const steps = numberOf(
    config.max_steps_per_turn,
    defaults.max_steps_per_turn,
  );
  const command = numberOf(
    config.command_timeout_s,
    defaults.command_timeout_s,
  );
  const toolCall = numberOf(
    config.tool_call_timeout_s,
    defaults.tool_call_timeout_s,
  );
  const half = (value) => Math.max(1, Math.round(value / 2));

  const lead = document.createElement("p");
  lead.innerHTML =
    "<b>Allegro mode</b> makes Syna work faster: less reasoning," +
    " shorter waits, and a tighter step budget.";
  allegroNote.appendChild(lead);

  const rows = [
    ["Thinking effort", "capped at low"],
    ["Request pause", "2 s → 1 s"],
    ["Steps per turn", `${steps} → ${half(steps)}`],
    ["Command timeout", `${command} s → ${half(command)} s`],
    ["Tool call timeout", `${toolCall} s → ${half(toolCall)} s`],
  ];
  const list = document.createElement("ul");
  for (const [label, value] of rows) {
    const item = document.createElement("li");
    const term = document.createElement("span");
    term.className = "settings-note-term";
    term.textContent = label;
    item.appendChild(term);
    item.appendChild(document.createTextNode(value));
    list.appendChild(item);
  }
  allegroNote.appendChild(list);
  cat6Body.appendChild(allegroNote);
  cat6.appendChild(cat6Body);
  body.appendChild(cat6);

  modal.appendChild(body);

  // Footer with Save and Restart
  const footer = document.createElement("div");
  footer.className = "settings-modal-footer";

  const saveBtn = document.createElement("button");
  saveBtn.className = "settings-save-btn";
  saveBtn.textContent = "Save and Restart";
  saveBtn.disabled = true;

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveBtn.innerHTML = SPINNER_SVG + "<span>Restarting...</span>";
    try {
      const res = await fetch(withToken(API_BASE + "/api/settings"), {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ env, config }),
      });
      if (!res.ok) throw new Error("Server returned " + res.status);
      waitForRestartThenReload();
    } catch (err) {
      alert("Failed to save settings: " + err.message);
      saveBtn.disabled = false;
      saveBtn.textContent = "Save and Restart";
    }
  });

  footer.appendChild(saveBtn);
  modal.appendChild(footer);

  backdrop.appendChild(modal);
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) closeSettingsModal();
  });

  document.body.appendChild(backdrop);
  requestAnimationFrame(() => {
    requestAnimationFrame(() => backdrop.classList.add("open"));
  });
}

attachBtn.innerHTML = ICONS.clip;
newchatBtn.innerHTML = ICONS.newchat;
if (APP_VERSION) document.getElementById("brand-version").textContent = APP_VERSION;
if (settingsBtn) {
  settingsBtn.innerHTML = ICONS.settings;
  settingsBtn.addEventListener("click", openSettingsModal);
}
if (bgBtn) {
  bgBtn.innerHTML = ICONS.work;
  bgBtn.addEventListener("click", openBackgroundListModal);
}
startBgListPolling();
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
