"use strict";

// Chat client: WebSocket transport, conversation rendering (bubbles, tool rows,
// file chips, events, compaction) and the composer. Visual state classes are
// styled in css/chat.css; all entrance/exit motion runs through the WAAPI
// helpers below so nothing on the page ever snaps.

const chat = document.getElementById("chat");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const attachBtn = document.getElementById("attach-btn");
const fileInput = document.getElementById("file-input");
const statusRow = document.createElement("div");
statusRow.className = "inline-status glass";
statusRow.innerHTML = '<span class="status-dot"></span><span class="status-label"></span>';
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

const isServed = location.protocol === "http:" || location.protocol === "https:";
const API_BASE = isServed ? location.origin : "http://127.0.0.1:8000";
const WS_BASE = isServed
  ? (location.protocol === "https:" ? "wss://" : "ws://") + location.host
  : "ws://127.0.0.1:8000";

let ws = null;
let reconnectDelay = 1000;
let busy = false;

const REDUCE = matchMedia("(prefers-reduced-motion: reduce)").matches;
const EASE_OUT = "cubic-bezier(0.22, 1, 0.36, 1)";
const EASE_IN = "cubic-bezier(0.65, 0, 0.35, 1)";

// ---------- icons ----------
// All line icons share one attribute block; stroke color comes from CSS.
// Icons that get stacked/crossfaded inside a button take a class (cls).
const I = (inner, cls) =>
  '<svg ' + (cls ? 'class="' + cls + '" ' : '') + 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + inner + "</svg>";

const ICONS = {
  send: I('<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/>'),
  stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  clip: I('<path d="M21.44 11.05 12.25 20.24a6 6 0 0 1-8.49-8.49l8.57-8.57a4 4 0 1 1 5.66 5.66l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>'),
  file: I('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>'),
  terminal: I('<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>'),
  search: I('<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>'),
  pencil: I('<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>'),
  eye: I('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>'),
  share: I('<path d="M12 3v12"/><path d="m7 8 5-5 5 5"/><path d="M5 21h14"/>'),
  warn: I('<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),
  copy: I('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>', "i-copy"),
  check: I('<path d="M20 6 9 17l-5-5"/>', "i-check")
};

const TOOL_META = {
  execute_command: { verb: "Ran command", icon: ICONS.terminal, arg: 0 },
  web_search: { verb: "Searched", icon: ICONS.search, arg: 0 },
  read_file: { verb: "Read file", icon: ICONS.eye, arg: 0 },
  edit_file: { verb: "Edited file", icon: ICONS.pencil, arg: 0 },
  share_file: { verb: "Share file", icon: ICONS.share, arg: 0 }
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
  if (host._anim) { host._anim.onfinish = null; host._anim.cancel(); host._anim = null; }
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
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  // Fallback for non-secure contexts (e.g. plain http over the network)
  return new Promise((resolve, reject) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy") ? resolve() : reject(new Error("copy rejected")); }
    catch (err) { reject(err); }
    finally { ta.remove(); }
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
    copyText(source).then(() => {
      btn.classList.add("copied");
      clearTimeout(btn._t);
      btn._t = setTimeout(() => btn.classList.remove("copied"), COPY_FEEDBACK_MS);
    }).catch(() => {});
  });
  bubble.appendChild(btn);
}

// ---------- tool rows ----------
function toolEventText(event) {
  const meta = TOOL_META[event.name];
  if (!meta) return truncateArg(event.signature);
  const arg = truncateArg(firstQuotedArg(event.signature));
  let verb = meta.verb;
  if (event.name === "web_search" && /^https?:\/\//.test(firstQuotedArg(event.signature))) verb = "Visited";
  return verb + (arg ? ' "' + arg + '"' : "");
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

const CHEV = '<svg class="chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';

function ensureMoreRow(run) {
  if (!run.moreRow) {
    const row = document.createElement("div");
    row.className = "see-more";
    row.innerHTML =
      '<button type="button" class="see-btn glass"><span class="see-label"><span class="l-more">See more</span>' +
      '<span class="l-less">See less</span></span><span class="see-slot">' + CHEV +
      '<span class="jump-dots"><i></i><i></i><i></i></span></span></button>';
    row.querySelector("button").addEventListener("click", () =>
      run.expanded ? collapseToolRun(run) : expandToolRun(run));
    run.moreRow = row;
  }
  return run.moreRow;
}

// Rows animate height + margin + padding together, so everything below
// glides instead of snapping; the toggle itself never moves or resizes.
const rowFrame = (h, m, p, b, o, f, t) => ({
  height: h + "px", marginTop: m + "px", paddingTop: p + "px", paddingBottom: p + "px",
  borderTopWidth: b + "px", borderBottomWidth: b + "px", opacity: o, filter: f, transform: t
});
const ROW_OUT = "translateY(-6px) scale(0.985)";

function revealRow(row, delay) {
  cancelAnim(row);
  row.style.transition = "none"; row.style.overflow = "hidden";
  row.classList.remove("hidden");
  const h = row.getBoundingClientRect().height;
  const anim = row._anim = row.animate(
    [rowFrame(0, -10, 0, 0, 0, "blur(8px)", ROW_OUT), rowFrame(h, 0, 6, 1, 1, "blur(0px)", "none")],
    { duration: 560, delay, easing: EASE_OUT, fill: "backwards" });
  anim.onfinish = () => { row.style.transition = ""; row.style.overflow = ""; row._anim = null; };
}

function concealRow(row, delay) {
  if (row.classList.contains("hidden")) return;
  cancelAnim(row);
  row.style.transition = "none"; row.style.overflow = "hidden";
  const h = row.getBoundingClientRect().height;
  const anim = row._anim = row.animate(
    [rowFrame(h, 0, 6, 1, 1, "blur(0px)", "none"), rowFrame(0, -10, 0, 0, 0, "blur(8px)", ROW_OUT)],
    { duration: 380, delay, easing: EASE_IN, fill: "forwards" });
  anim.onfinish = () => {
    row.classList.add("hidden");
    row.style.transition = ""; row.style.overflow = "";
    anim.cancel(); row._anim = null;
  };
}

let followUntil = 0, following = false;
function followBottom(ms) {
  followUntil = Math.max(followUntil, performance.now() + ms);
  if (following) return;
  following = true;
  (function step() {
    scrollToBottom();
    if (performance.now() < followUntil) requestAnimationFrame(step); else following = false;
  })();
}

const BOX_ZERO = { height: "0px", paddingTop: "0px", paddingBottom: "0px", borderTopWidth: "0px", borderBottomWidth: "0px" };
const boxOf = (el, cs) => ({ height: el.getBoundingClientRect().height + "px", paddingTop: cs.paddingTop,
  paddingBottom: cs.paddingBottom, borderTopWidth: cs.borderTopWidth, borderBottomWidth: cs.borderBottomWidth });

// New elements grow from zero height so nothing around them snaps.
function growIn(el, delay) {
  if (REDUCE || !el.isConnected || !el.getBoundingClientRect().height) return;
  const cs = getComputedStyle(el);
  const gap = parseFloat(getComputedStyle(chat).rowGap) || 0;
  el.style.overflow = "hidden";
  const a = el.animate(
    [{ ...BOX_ZERO, marginTop: -gap + "px" }, { ...boxOf(el, cs), marginTop: cs.marginTop }],
    { duration: 520, delay: delay || 0, easing: EASE_OUT, fill: "backwards" });
  a.onfinish = () => { el.style.overflow = ""; };
}

// Tool-call details: the output panel animates height + margin + fade, so the
// row (and everything below it) resizes continuously.
function toggleOpen(row) {
  const pre = row.querySelector("pre");
  if (!pre || REDUCE) { Sfx.play(row.classList.contains("open") ? "close" : "open"); row.classList.toggle("open"); return; }
  cancelAnim(pre);
  const opening = !row.classList.contains("open");
  Sfx.play(opening ? "open" : "close");
  const gap = parseFloat(getComputedStyle(row).rowGap) || 0;
  if (opening) row.classList.add("open");
  pre.style.overflow = "hidden";
  const cs = getComputedStyle(pre);
  const shown = { ...boxOf(pre, cs), marginTop: cs.marginTop, opacity: 1, filter: "blur(0px)", transform: "none" };
  const hidden = { ...BOX_ZERO, marginTop: -gap + "px", opacity: 0, filter: "blur(8px)", transform: "translateY(3px)" };
  const anim = pre._anim = pre.animate(opening ? [hidden, shown] : [shown, hidden],
    { duration: opening ? 520 : 380, easing: opening ? EASE_OUT : EASE_IN, fill: opening ? "backwards" : "forwards" });
  anim.onfinish = () => {
    if (!opening) row.classList.remove("open");
    pre.style.overflow = ""; anim.cancel(); pre._anim = null;
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

function expandToolRun(run) {
  run.expanded = true;
  Sfx.play("open");
  run.moreRow.classList.add("expanded");
  const stick = isAtBottom();
  run.rows.filter((r) => r.classList.contains("hidden"))
    .forEach((row, i) => revealRow(row, Math.min(i * 45, 270)));
  updateToolRunThinking();
  if (stick) followBottom(900);
}

function collapseToolRun(run) {
  run.expanded = false;
  Sfx.play("close");
  run.moreRow.classList.remove("expanded");
  const stick = isAtBottom();
  run.rows.slice(MAX_VISIBLE_TOOL_CALLS).forEach((row, i) => concealRow(row, Math.min(i * 30, 150)));
  updateToolRunThinking();
  if (stick) followBottom(700);
}

function addToolRow(event) {
  const meta = TOOL_META[event.name];
  const row = document.createElement("div");
  row.className = "tool-row glass running";
  row.id = "tool-" + event.id;
  const icon = meta ? meta.icon : ICONS.terminal;
  row.innerHTML =
    '<span class="tool-icon">' + icon + "</span>" +
    '<span class="tool-text"></span>' +
    "<pre hidden></pre>";
  row.querySelector(".tool-text").textContent = toolEventText(event);
  if (!toolRun) {
    toolRun = { rows: [], moreRow: null, expanded: false };
  }
  toolRun.rows.push(row);
  const stick = isAtBottom();
  if (!toolRun.expanded && toolRun.rows.length > MAX_VISIBLE_TOOL_CALLS) {
    row.classList.add("hidden");
    const moreRow = ensureMoreRow(toolRun);
    if (moreRow.parentNode === chat) {
      // Insert ahead of the existing row without re-appending it, so the
      // "See more" pill does not replay its entrance on every new call.
      chat.insertBefore(row, moreRow);
    } else {
      append(row);
      append(moreRow);
    }
    updateToolRunThinking();
  } else if (toolRun.expanded && toolRun.moreRow && toolRun.moreRow.parentNode === chat) {
    chat.insertBefore(row, toolRun.moreRow);
    growIn(row);
  } else {
    append(row);
  }
  if (stick) scrollToBottom();
  updateAmbient();
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
    '<span class="tool-icon">' + ICONS.warn + "</span>" +
    '<span class="tool-text">Tool call failed: "' + DECODE_ERROR_PREFIX +
    '"</span>' +
    "<pre hidden></pre>";
  row.querySelector("pre").textContent = detail;
  row.addEventListener("click", () => toggleOpen(row));
  append(row);
}

function addFileChip(event) {
  closeFileGroup();
  const chipHost = document.createElement("div");
  chipHost.className =
    "files-group open" + (event.type === "file_sent" ? " right" : "");
  const chip = document.createElement("a");
  chip.className = "file-chip glass" + (event.type === "file_sent" ? " outgoing" : "");
  chip.href = event.url ? API_BASE + event.url : "#";
  if (event.url) chip.setAttribute("download", event.name);
  chip.innerHTML = ICONS.file +
    "<span></span><span class='file-meta'></span>";
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
  statusLabel.textContent = label;
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
        [{ ...boxOf(statusRow, cs), marginTop: "0px" }, { ...BOX_ZERO, marginTop: "-10px" }],
        { duration: 480, easing: EASE_IN, fill: "forwards" });
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
  const kick = { user_message: 1, model_message: 0.8, busy: 0.6, ready: 0.7, error: 1,
    tool_call: event.status === "started" ? 0.35 : 0.5 }[event.type];
  if (kick && window.ambientKick) window.ambientKick(kick);
  const snd = { model_message: "receive", error: "error",
    tool_call: event.status === "started" ? "tick" : event.failed ? "error" : "done" }[event.type];
  if (snd) Sfx.play(snd);
  if (event.type !== "file" && event.type !== "file_sent") closeFileGroup();
  if (
    event.type !== "tool_call" &&
    event.type !== "status" &&
    event.type !== "busy" &&
    event.type !== "ready"
  ) {
    resetToolRun();
  }
  switch (event.type) {
    case "user_message":
      addBubble(event.content, "user", event.queued);
      break;
    case "model_message":
      addBubble(event.content, "model", false);
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
    setStatus("ready", "Ready");
    updateAmbient();
  };

  ws.onmessage = (message) => {
    try { render(JSON.parse(message.data)); } catch (err) { console.error(err); }
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
  const mode = busy ? "stop" : "send";
  if (sendBtn.dataset.mode !== mode) {
    sendBtn.dataset.mode = mode;
    sendBtn.classList.remove("pop"); void sendBtn.offsetWidth; sendBtn.classList.add("pop");
  }
  document.querySelector(".composer").classList.toggle("busy", busy);
  if (busy) {
    sendBtn.innerHTML = ICONS.stop;
    sendBtn.title = "Stop";
    sendBtn.disabled = false;
  } else {
    sendBtn.innerHTML = ICONS.send;
    sendBtn.title = "Send";
    sendBtn.disabled = !input.value.trim();
  }
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

function uploadFiles(files) {
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    fetch(API_BASE + "/api/upload/" + clientId, { method: "POST", body: form })
      .then((response) => {
        if (!response.ok) throw new Error("Upload failed (" + response.status + ")");
      })
      .catch((err) => addEventRow("Could not send " + file.name + ": " + err.message, true));
  }
}

// ---------- wiring & boot ----------
attachBtn.innerHTML = ICONS.clip;
setSendState();
input.focus();

sendBtn.addEventListener("click", () => (busy ? (Sfx.play("stop"), sendStop()) : sendMessage()));
attachBtn.addEventListener("click", () => fileInput.click());
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

connect();
