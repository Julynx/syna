"use strict";

// Synthesized UI sounds: every note is a small chord of detuned partials plus a
// filtered noise "tick". Loudness drifts smoothly between plays, sounds in the
// same category crossfade into each other, and the master level ramps (never cuts).
const Sfx = (() => {
  // Volume tiers (g): thinking < tool < user actions < sent message.
  // Pitches stay on root / fifth / octave only, so nothing forms a tune.
  const S = {
    // user actions: middle
    click:   { n: [[0, 0], [7, 0.012]], g: 0.13, d: 0.12, f: 3400, tr: 0.12, vary: 1, cat: "ui", gap: 0.03 },
    hover:   { n: [[12, 0]], g: 0.02, d: 0.05, f: 2600, tr: 0.03, cat: "hov", gap: 0.06 },
    open:    { n: [[0, 0], [7, 0.05]], g: 0.12, d: 0.24, f: 3200, tr: 0.08, cat: "ui", gap: 0.1 },
    close:   { n: [[7, 0], [0, 0.05]], g: 0.11, d: 0.22, f: 2800, tr: 0.06, cat: "ui", gap: 0.1 },
    stop:    { n: [[7, 0], [0, 0.05]], g: 0.13, d: 0.2, f: 2800, tr: 0.08, cat: "ui", gap: 0.1 },
    // messages: sent is the loudest sound in the app
    send:    { n: [[0, 0], [7, 0.04], [12, 0.08]], g: 0.3, d: 0.3, f: 3600, tr: 0.14, cat: "msg", gap: 0.1 },
    receive: { n: [[12, 0], [7, 0.07]], g: 0.15, d: 0.38, f: 2800, tr: 0.04, cat: "msg", gap: 0.1 },
    // tool events
    tick:    { n: [[7, 0]], g: 0.06, d: 0.08, f: 2800, tr: 0.07, vary: 1, cat: "tool", gap: 0.03 },
    done:    { n: [[0, 0], [7, 0], [12, 0.05]], g: 0.09, d: 0.45, f: 2800, tr: 0.03, cat: "tool", gap: 0.1 },
    error:   { n: [[-5, 0], [1, 0.06]], g: 0.12, d: 0.4, f: 1800, tr: 0.03, cat: "tool", gap: 0.1 },
    // activity layer (no category: notes overlap and ring out naturally)
    think1:  { n: [[0, 0], [7, 0]], g: 0.03, d: 0.5, f: 2400, tr: 0.03, gap: 0 },
    think3:  { n: [[0, 0]], g: 0.03, d: 0.3, f: 2800, tr: 0.07, vary: 1, gap: 0 },
    beat:    { n: [[0, 0], [7, 0.17]], g: 0.055, d: 0.24, f: 2600, tr: 0.09, gap: 0 }
  };
  const ROOTS = { idle: 293.66, thinking: 329.63, tool: 261.63 };
  const LEVEL = 0.55;
  let ctx = null, master, bus, noise, level = 0.8, on = true;
  const last = {}, active = {}, acts = {};
  let cur = "", timer = null;
  try { on = localStorage.getItem("syna-sound") !== "off"; } catch (e) {}

  function unlock() {
    if (!ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      ctx = new AC();
      const comp = ctx.createDynamicsCompressor();
      comp.threshold.value = -12; comp.ratio.value = 4; comp.attack.value = 0.003; comp.release.value = 0.2;
      master = ctx.createGain(); master.gain.value = on ? LEVEL : 0;
      bus = ctx.createGain();
      bus.connect(comp); comp.connect(master); master.connect(ctx.destination);
      // soft room: filtered feedback delay
      const dl = ctx.createDelay(); dl.delayTime.value = 0.11;
      const fb = ctx.createGain(); fb.gain.value = 0.28;
      const wet = ctx.createGain(); wet.gain.value = 0.22;
      const lp = ctx.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.value = 2400;
      bus.connect(dl); dl.connect(lp); lp.connect(fb); fb.connect(dl); lp.connect(wet); wet.connect(comp);
      noise = ctx.createBuffer(1, Math.floor(ctx.sampleRate * 0.06), ctx.sampleRate);
      const d = noise.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
      for (const k of ["thinking", "tool"]) { // per-state activity buses, silent until needed
        acts[k] = ctx.createGain(); acts[k].gain.value = 0; acts[k].connect(bus);
      }
      cur = ""; sync();
    }
    if (ctx.state === "suspended") ctx.resume();
  }

  function play(name, dest) {
    const def = S[name];
    if (!def || !ctx || !on || ctx.state !== "running") return;
    const now = ctx.currentTime;
    if (now - (last[name] || 0) < def.gap) return;
    last[name] = now;
    level += (0.55 + 0.45 * Math.random() - level) * 0.4; // smooth loudness drift
    const root = ROOTS[document.body.dataset.ambient || "idle"];
    const shift = def.vary ? [0, 7, 12][(Math.random() * 3) | 0] : 0;
    const peak = def.g * level;

    const pan = ctx.createStereoPanner(); pan.pan.value = (Math.random() - 0.5) * 0.5; pan.connect(dest || bus);
    const env = ctx.createGain(); env.connect(pan);
    const prev = def.cat && active[def.cat];
    if (prev) { // crossfade the previous sound of this category out
      prev.gain.setValueAtTime(prev.gain.value, now);
      prev.gain.setTargetAtTime(0, now, 0.03);
    }
    if (def.cat) active[def.cat] = env;
    env.gain.setValueAtTime(0, now);
    env.gain.linearRampToValueAtTime(1, now + 0.015);
    const lp = ctx.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.value = def.f; lp.Q.value = 0.5; lp.connect(env);

    let end = 0;
    for (const [semi, off] of def.n) {
      const f0 = root * 2 ** ((semi + shift) / 12), t = now + 0.004 + off;
      end = Math.max(end, off + def.d);
      for (const [m, type, a] of [[1, "sine", 1], [2, "triangle", 0.28], [3.01, "sine", 0.1]]) {
        const o = ctx.createOscillator(); o.type = type; o.frequency.value = f0 * m;
        o.detune.value = (Math.random() - 0.5) * 10;
        const g = ctx.createGain();
        g.gain.setValueAtTime(0, t);
        g.gain.linearRampToValueAtTime(peak * a, t + 0.006);
        g.gain.exponentialRampToValueAtTime(0.0001, t + def.d);
        o.connect(g); g.connect(lp); o.start(t); o.stop(t + def.d + 0.05);
      }
    }
    if (def.tr) { // soft filtered noise transient = the "click"
      const src = ctx.createBufferSource(); src.buffer = noise;
      const bp = ctx.createBiquadFilter(); bp.type = "bandpass"; bp.frequency.value = 3500 + Math.random() * 2000; bp.Q.value = 1.2;
      const g = ctx.createGain();
      g.gain.setValueAtTime(0, now);
      g.gain.linearRampToValueAtTime(def.tr * level, now + 0.002);
      g.gain.exponentialRampToValueAtTime(0.0001, now + 0.03);
      src.connect(bp); bp.connect(g); g.connect(env); src.start(now + 0.004);
    }
    setTimeout(() => { pan.disconnect(); if (active[def.cat] === env) delete active[def.cat]; }, (end + 1) * 1000);
  }

  // Activity layer: clicks/polytones only while the model is working. Each state
  // has its own bus; switching crossfades them, and idle fades everything to silence.
  function sync() {
    if (!ctx) return;
    const st = document.body.dataset.ambient || "idle";
    if (st === cur) return;
    cur = st;
    for (const k in acts) acts[k].gain.setTargetAtTime(k === st ? 1 : 0, ctx.currentTime, k === st ? 0.3 : 0.5);
    clearTimeout(timer);
    if (st !== "idle") loop();
  }
  function loop() {
    if (!on || !ctx || cur === "idle") return;
    if (ctx.state !== "running") { timer = setTimeout(loop, 500); return; }
    if (cur === "tool") {
      play("beat", acts.tool);
      timer = setTimeout(loop, 1150);
    } else {
      play(["think1", "think3", "think3"][(Math.random() * 3) | 0], acts.thinking);
      timer = setTimeout(loop, 450 + Math.random() * 900);
    }
  }
  new MutationObserver(sync).observe(document.body, { attributes: true, attributeFilter: ["data-ambient"] });

  function setOn(v) {
    on = v;
    try { localStorage.setItem("syna-sound", v ? "on" : "off"); } catch (e) {}
    unlock();
    if (ctx) master.gain.setTargetAtTime(v ? LEVEL : 0, ctx.currentTime, 0.12);
    cur = ""; sync();
    if (v && ctx) ctx.resume().then(() => play("click"));
  }
  return { unlock, play, setOn, get on() { return on; } };
})();

// Sound toggle button: stacks a muted/unmuted icon pair and flips between them.
(function () {
  const btn = document.getElementById("sound-btn");
  const paint = () => btn.classList.toggle("muted", !Sfx.on);
  btn.innerHTML =
    '<svg class="s-on" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a10 10 0 0 1 0 14"/></svg>' +
    '<svg class="s-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="m22 9-6 6"/><path d="m16 9 6 6"/></svg>';
  paint();
  btn.addEventListener("click", () => { Sfx.setOn(!Sfx.on); paint(); });
  ["pointerdown", "keydown"].forEach((ev) => addEventListener(ev, () => Sfx.unlock(), { capture: true }));
  document.addEventListener("click", (e) => {
    if (e.target.closest("#send-btn, #sound-btn, .see-btn, .tool-row")) return;
    if (e.target.closest("button, a.file-chip")) Sfx.play("click");
  });
  let hovered = null;
  document.addEventListener("pointerover", (e) => {
    if (e.pointerType !== "mouse") return;
    const t = e.target.closest(".see-btn, .icon-btn, .copy-btn, #send-btn:not(:disabled), .tool-row.toggleable, .file-chip");
    if (t && t !== hovered) Sfx.play("hover");
    hovered = t;
  });
})();
