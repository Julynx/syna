"use strict";

const Voice = (() => {
  const settings = (window.SYNA_CONFIG && window.SYNA_CONFIG.voice) || {};
  const SILENCE_TIMEOUT_MS = settings.silence_timeout_ms || 2000;
  const SILENCE_THRESHOLD_DBFS = Number.isFinite(
    settings.silence_threshold_dbfs,
  )
    ? settings.silence_threshold_dbfs
    : -45;
  const MAX_RECORDING_MS = settings.max_recording_ms || 60000;
  const STT_AVAILABLE = !!settings.stt_available;
  const TTS_AVAILABLE = !!settings.tts_available;
  const initialEnabled = settings.voice_mode_on === true;

  const micBtn = document.getElementById("mic-btn");
  const voiceBtn = document.getElementById("voice-btn");

  let enabled = initialEnabled;
  let recorder = null;
  let recorderStream = null;
  let meterContext = null;
  let analyser = null;
  let meterHandle = null;
  let recordingStartedAt = 0;
  let silenceStartedAt = 0;
  let recordingAborted = false;
  let playback = null;
  let playbackUrl = null;
  let speaking = false;
  let pillEl = null;

  function reset() {
    enabled = initialEnabled;
    stopPlayback();
    cancelRecording();
    hidePill();
    paint();
  }

  function paint() {
    voiceBtn.classList.toggle("muted", !enabled);
    voiceBtn.disabled = !(STT_AVAILABLE || TTS_AVAILABLE);
    voiceBtn.title = enabled ? "Disable voice mode" : "Enable voice mode";
    document.body.classList.toggle("voice-off", !enabled || !STT_AVAILABLE);
  }

  function speakableText(markdown) {
    return markdown
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/^\s{0,3}#{1,6}\s+/gm, "")
      .replace(/^\s*[-*+]\s+/gm, "")
      .replace(/^\s*\|.*\|\s*$/gm, "")
      .replace(/(\*\*|__|\*|_|~~)/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function showPill(label) {
    pillEl = document.createElement("div");
    pillEl.className = "inline-status glass voice-status";
    pillEl.innerHTML =
      '<span class="status-dot working"></span><span class="status-label"></span>';
    pillEl.querySelector(".status-label").textContent = label;
    const stick = isAtBottom();
    chat.insertBefore(pillEl, statusRow.parentNode === chat ? statusRow : null);
    growIn(pillEl);
    if (stick) followBottom(700);
  }

  function hidePill() {
    const pill = pillEl;
    pillEl = null;
    if (!pill || pill.parentNode !== chat) return;
    if (REDUCE) {
      pill.remove();
      return;
    }
    pill.getAnimations().forEach((anim) => anim.cancel());
    pill.style.overflow = "hidden";
    const cs = getComputedStyle(pill);
    const anim = pill.animate(
      [
        { ...boxOf(pill, cs), marginTop: cs.marginTop },
        { ...BOX_ZERO, marginTop: "-10px" },
      ],
      { duration: 420, easing: EASE_IN, fill: "forwards" },
    );
    anim.onfinish = () => pill.remove();
  }

  function setSpeaking(next) {
    if (speaking === next) return;
    speaking = next;
    if (next) hidePill();
    updateAmbient();
  }

  function stopPlayback() {
    if (playback) {
      playback.pause();
      playback = null;
    }
    if (playbackUrl) {
      URL.revokeObjectURL(playbackUrl);
      playbackUrl = null;
    }
    voiceBtn.classList.remove("speaking");
    setSpeaking(false);
  }

  function willNarrate(rawText) {
    return enabled && TTS_AVAILABLE && !!speakableText(String(rawText || ""));
  }

  async function speak(rawText) {
    const text = speakableText(String(rawText || ""));
    if (!enabled || !TTS_AVAILABLE || !text) {
      hidePill();
      return;
    }
    stopPlayback();
    try {
      const response = await fetch(API_BASE + "/api/speech/synthesize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
      });
      const payload = response.ok
        ? null
        : await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(
          (payload && payload.detail) || "HTTP " + response.status,
        );
      }
      playbackUrl = URL.createObjectURL(await response.blob());
      playback = new Audio(playbackUrl);
      playback.onended = () => {
        voiceBtn.classList.remove("speaking");
        setSpeaking(false);
      };
      voiceBtn.classList.add("speaking");
      setSpeaking(true);
      await playback.play();
    } catch (err) {
      voiceBtn.classList.remove("speaking");
      setSpeaking(false);
      hidePill();
      console.error(err);
      addEventRow("Speech playback failed: " + err.message, true);
    }
  }

  function pickRecorderMime() {
    if (!window.MediaRecorder) return "";
    const candidates = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/ogg",
      "audio/mp4",
    ];
    return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || "";
  }

  function extensionFor(mime) {
    if (String(mime).includes("ogg")) return "ogg";
    if (String(mime).includes("mp4")) return "m4a";
    return "webm";
  }

  function resetRecorderState() {
    clearInterval(meterHandle);
    meterHandle = null;
    analyser = null;
    if (meterContext) {
      meterContext.close().catch(() => {});
      meterContext = null;
    }
    if (recorderStream) {
      recorderStream.getTracks().forEach((track) => track.stop());
      recorderStream = null;
    }
    recorder = null;
    micBtn.classList.remove("recording");
    hidePill();
  }

  function rmsDbfs(samples) {
    let sum = 0;
    for (const sample of samples) sum += sample * sample;
    return 20 * Math.log10(Math.sqrt(sum / samples.length) || 1e-10);
  }

  function meterLoop() {
    const buffer = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(buffer);
    const now = performance.now();
    if (now - recordingStartedAt >= MAX_RECORDING_MS) return stopRecording();
    if (rmsDbfs(buffer) < SILENCE_THRESHOLD_DBFS) {
      if (!silenceStartedAt) silenceStartedAt = now;
      else if (now - silenceStartedAt >= SILENCE_TIMEOUT_MS) stopRecording();
    } else {
      silenceStartedAt = 0;
    }
  }

  async function startRecording() {
    stopPlayback();
    try {
      recorderStream = await navigator.mediaDevices.getUserMedia({
        audio: true,
      });
    } catch (err) {
      console.error(err);
      addEventRow(
        err.name === "NotAllowedError"
          ? "Microphone access was denied."
          : "Could not open the microphone: " + err.message,
        true,
      );
      return;
    }
    const mimeType = pickRecorderMime();
    const chunks = [];
    recorder = new MediaRecorder(
      recorderStream,
      mimeType ? { mimeType: mimeType } : undefined,
    );
    recorder.ondataavailable = (event) => {
      if (event.data.size) chunks.push(event.data);
    };
    recorder.onstop = async () => {
      const blob = new Blob(chunks, {
        type: recorder.mimeType || mimeType || "audio/webm",
      });
      const aborted = recordingAborted;
      resetRecorderState();
      if (!aborted) await transcribeAndSend(blob);
    };
    meterContext = new AudioContext();
    const source = meterContext.createMediaStreamSource(recorderStream);
    analyser = meterContext.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    meterContext.resume().catch(() => {});
    recorder.start();
    recordingStartedAt = performance.now();
    silenceStartedAt = 0;
    recordingAborted = false;
    micBtn.classList.add("recording");
    meterHandle = setInterval(meterLoop, 100);
    showPill("Listening...");
  }

  function stopRecording() {
    if (recorder && recorder.state !== "inactive") recorder.stop();
  }

  function cancelRecording() {
    if (!recorder) return;
    recordingAborted = true;
    stopRecording();
  }

  async function transcribeAndSend(blob) {
    micBtn.disabled = true;
    micBtn.title = "Transcribing...";
    try {
      const form = new FormData();
      form.append("file", blob, "voice-note." + extensionFor(blob.type));
      const response = await fetch(API_BASE + "/api/speech/transcribe", {
        method: "POST",
        body: form,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok)
        throw new Error(payload.detail || "HTTP " + response.status);
      const text = (payload.text || "").trim();
      if (!text) {
        addEventRow("Nothing was heard in the recording.", true);
        return;
      }
      input.value = text;
      sendMessage();
    } catch (err) {
      console.error(err);
      addEventRow("Transcription failed: " + err.message, true);
    } finally {
      micBtn.disabled = false;
      micBtn.title = "Talk to Syna";
    }
  }

  micBtn.innerHTML = ICONS.mic;
  voiceBtn.innerHTML = ICONS.speakerOn + ICONS.speakerOff;
  paint();

  voiceBtn.addEventListener("click", () => {
    enabled = !enabled;
    if (!enabled) {
      stopPlayback();
      cancelRecording();
    }
    paint();
  });

  micBtn.addEventListener("click", () =>
    recorder ? stopRecording() : startRecording(),
  );

  addEventListener("beforeunload", () => {
    cancelRecording();
    stopPlayback();
  });

  return {
    speak,
    stopPlayback,
    reset,
    showPill,
    hidePill,
    willNarrate,
    get enabled() {
      return enabled;
    },
    get speaking() {
      return speaking;
    },
  };
})();

window.Voice = Voice;
