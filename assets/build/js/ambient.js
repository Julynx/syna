"use strict";

// Maps app activity onto the background light field:
//   idle     -> waiting for the user
//   thinking -> user sent a message, awaiting the model
//   tool     -> waiting for a tool call to complete
// Reads the global `busy` flag owned by js/chat.js.
let ambientState = "";
function updateAmbient() {
  const toolActive = document.querySelector(".tool-row.running") !== null;
  const next = !busy ? "idle" : toolActive ? "tool" : "thinking";
  if (next === ambientState) return;
  ambientState = next;
  document.body.setAttribute("data-ambient", next);
}

// Glow: a bright core with a corona. The corona is ~22 stacked translucent shapes whose
// outlines are r(theta) = R * (1 + spread * f^1.5 * (1 + amp * d(theta))), where d is a
// smoothed sum of random sine waves. Rendered small, upscaled and blurred = heavy diffusion.
(function () {
  const root = document.querySelector(".ambient");
  const field = root.querySelector(".field");
  const cv = document.createElement("canvas");
  cv.className = "glow";
  field.appendChild(cv);
  const c = cv.getContext("2d");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const dark = matchMedia("(prefers-color-scheme: dark)");
  const TAU = Math.PI * 2, N = 180, L = 22;
  const PAL = {
    idle: [[167, 139, 250], [240, 160, 192], [124, 196, 255]],      // violet / rose / sky, merged
    thinking: [[255, 168, 96], [255, 207, 122], [255, 138, 122]],   // soft orange dominant
    tool: [[91, 155, 255], [127, 208, 255], [122, 124, 255]]        // mostly blue
  };
  const MODE = { idle: { gain: 1, speed: 1, amp: 0.4, spread: 1 },
    thinking: { gain: 2, speed: 2.4, amp: 0.8, spread: 1.15 },
    tool: { gain: 1.3, speed: 1.5, amp: 0.55, spread: 1.02 } };
  const col = PAL.idle.map((x) => x.slice());
  const W = { idle: 1, thinking: 0, tool: 0 };
  const cur = { gain: 1, speed: 1, amp: 0.4, spread: 1 };
  // random harmonics: integer k so the outline closes; low k dominate (smooth), each drifts at its own pace
  const SIN = [1, 2, 3, 5, 7, 10].map((k, i) => ({ k, i, a: 1 / k ** 0.7, w: (0.12 + Math.random() * 0.3) * (Math.random() < 0.5 ? -1 : 1), p: Math.random() * TAU }));
  const norm = SIN.reduce((n, x) => n + x.a, 0);
  const d = new Float32Array(N);
  const g = (x, w) => Math.exp(-((x / w) ** 2));
  let ks = 0, kv = 0, t = 0, last = performance.now(), pulse = 0;

  function resize() {
    cv.width = Math.max(200, Math.round(innerWidth * 0.26));
    cv.height = Math.max(200, Math.round(innerHeight * 0.26));
    if (reduce) draw(0.016);
  }
  addEventListener("resize", resize);

  function ring(s) {
    const r = document.createElement("div");
    r.className = "pulse-ring";
    root.appendChild(r);
    r.animate([{ transform: "scale(0.35)", opacity: 0.55 * s }, { transform: "scale(1.9)", opacity: 0 }],
      { duration: 1900, easing: "cubic-bezier(0.16, 1, 0.3, 1)" }).onfinish = () => r.remove();
  }
  window.ambientKick = reduce ? () => {} : (k) => { kv += 4 * k; ring(k); };

  function draw(dt) {
    t += dt;
    const st = document.body.dataset.ambient || "idle";
    for (const k in W) W[k] += ((k === st ? 1 : 0) - W[k]) * Math.min(1, dt * 1.8);
    const m = MODE[st], ease = 1 - Math.exp(-dt * 1.2);
    for (const k in cur) cur[k] += (m[k] - cur[k]) * ease;
    PAL[st].forEach((tc, i) => { for (let j = 0; j < 3; j++) col[i][j] += (tc[j] - col[i][j]) * ease; });
    // breathing / state pulse
    const ph = (t % 1.15) / 1.15;
    const idle = 0.5 + 0.5 * Math.sin(t * TAU / 8);
    const think = (0.5 + 0.5 * Math.sin(t * TAU / 2.4)) * (0.75 + 0.25 * Math.sin(t * TAU / 5.3));
    const beat = Math.min(1, g(Math.min(ph, 1 - ph), 0.08) + 0.65 * g(ph - 0.22, 0.07));
    pulse = W.idle * idle + W.thinking * think + W.tool * beat;
    kv += (-90 * ks - 9 * kv) * dt; ks += kv * dt;
    // one corona per colour. Each is the same random-sine outline, but time-shifted (tau) and
    // radially scaled (scl) -> radial chromatic aberration; additive blend in dark mode
    for (const h of SIN) h.p += h.w * cur.speed * dt;
    const W_ = cv.width, H_ = cv.height, cx = W_ / 2, cy = H_ / 2;
    const R = Math.min(W_, H_) * 0.2;
    const sp = cur.spread * (1 + 0.13 * pulse + 0.2 * ks);
    const isDark = dark.matches;
    const alpha = (isDark ? 0.05 : 0.09) * (0.85 + 0.3 * pulse + 0.4 * ks);
    const rgb = (x) => "rgb(" + (x[0] | 0) + "," + (x[1] | 0) + "," + (x[2] | 0) + ")";
    c.clearRect(0, 0, W_, H_);
    c.globalCompositeOperation = isDark ? "lighter" : "source-over";
    c.globalAlpha = alpha;
    for (let i = 0; i < 3; i++) {
      const tau = (i - 1) * 1.1, scl = 1 + (i - 1) * 0.12;
      d.fill(0);
      for (const h of SIN) {
        const amp = (h.a / norm) * (1 + (cur.gain - 1) * (h.i / 5)) * (1 + 0.4 * Math.sin(t * 0.2 + h.i * 1.7)) * 1.6;
        const phs = h.p - h.w * cur.speed * tau;
        for (let n = 0; n < N; n++) d[n] += amp * Math.sin(h.k * (n / N) * TAU + phs);
      }
      c.fillStyle = rgb(col[i]);
      for (let j = 1; j <= L; j++) {
        const f = j / L, ff = f ** 1.5;
        c.beginPath();
        for (let n = 0; n <= N; n++) {
          const a = (n % N) / N * TAU, r = R * (1 + sp * scl * ff * (1 + cur.amp * d[n % N]));
          const x = cx + r * Math.cos(a), y = cy + r * Math.sin(a);
          n ? c.lineTo(x, y) : c.moveTo(x, y);
        }
        c.fill();
      }
    }
    c.globalCompositeOperation = "source-over";
    c.globalAlpha = 1;
    // hot rim hugging the core
    const hot = col[0].map((v) => v + (255 - v) * 0.6);
    const rim = c.createRadialGradient(cx, cy, R * 0.96, cx, cy, R * 1.3);
    rim.addColorStop(0, "rgba(" + hot.map((v) => v | 0).join(",") + "," + (0.5 + 0.2 * pulse) + ")");
    rim.addColorStop(0.3, "rgba(" + hot.map((v) => v | 0).join(",") + ",0.16)");
    rim.addColorStop(1, "rgba(" + hot.map((v) => v | 0).join(",") + ",0)");
    c.fillStyle = rim; c.beginPath(); c.arc(cx, cy, R * 1.3, 0, TAU); c.fill();
    // the bright core: a hot near-white heart tinted with the state hue, breathing with
    // the same pulse as the corona; creamy falloff melts it into the surrounding light
    const coreA = (isDark ? 0.95 : 0.85) * (0.86 + 0.14 * pulse);
    const coreHi = col[0].map((v) => v + (255 - v) * 0.85);
    const coreLo = col[0].map((v) => v + (255 - v) * 0.4);
    const coreG = c.createRadialGradient(cx, cy, 0, cx, cy, R);
    coreG.addColorStop(0, "rgba(" + coreHi.map((v) => v | 0).join(",") + "," + coreA + ")");
    coreG.addColorStop(0.4, "rgba(" + coreHi.map((v) => v | 0).join(",") + "," + coreA * 0.62 + ")");
    coreG.addColorStop(0.75, "rgba(" + coreLo.map((v) => v | 0).join(",") + "," + coreA * 0.22 + ")");
    coreG.addColorStop(1, "rgba(" + col[0].map((v) => v | 0).join(",") + ",0)");
    c.fillStyle = coreG; c.beginPath(); c.arc(cx, cy, R, 0, TAU); c.fill();
  }
  function frame(now) {
    const dt = Math.max(0.001, Math.min((now - last) / 1000, 0.05));
    last = now; draw(dt);
    requestAnimationFrame(frame);
  }
  resize();
  if (!reduce) requestAnimationFrame(frame);
})();
