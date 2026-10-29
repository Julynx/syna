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
  const VOICE_ENABLED = settings.voice_enabled !== false;
  const initialEnabled = settings.voice_mode_on === true && VOICE_ENABLED;

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
  let pendingNarration = null;

  const NARRATION_REVEAL_TIMEOUT_MS = Number.isFinite(
    settings.narration_reveal_timeout_ms,
  )
    ? settings.narration_reveal_timeout_ms
    : 30000;
  const NARRATION_MAX_IN_FLIGHT = 2;

  function reset() {
    stop();
    enabled = initialEnabled;
    cancelRecording();
    hidePill();
    paint();
  }

  /* Cancel a narrated reply in flight and silence playback: abort every
     in-flight synthesis, discard queued audio, and stop the current
     chunk. Used by the composer's Stop button, new chat, and unload. */
  function stop() {
    cancelPendingNarration(false);
    stopPlayback();
  }

  function paint() {
    voiceBtn.classList.toggle("muted", !enabled);
    voiceBtn.disabled = !VOICE_ENABLED || !(STT_AVAILABLE || TTS_AVAILABLE);
    voiceBtn.title = !VOICE_ENABLED
      ? "Voice is disabled in the settings"
      : enabled
        ? "Disable voice mode"
        : "Enable voice mode";
    document.body.classList.toggle("voice-off", !enabled || !STT_AVAILABLE);
  }

  /**
   * Reduce a reply to plain spoken prose: markdown structure is
   * unwrapped, code fences and images are dropped, link labels survive
   * without their URLs, raw URLs are removed (the synthesizer must not
   * read "https://..."), LaTeX math is dropped (inline spans only when
   * they carry LaTeX syntax, so "$3.50 - $5" survives) and emoji,
   * including flags, skin tones and keycaps, are removed because the
   * synthesizer reads them unpredictably.
   */
  function speakableText(markdown) {
    return markdown
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/\$\$[^$]*\$\$/g, " ")
      .replace(/\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)/g, " ")
      .replace(/\$[^$\n]*[\\^_{}][^$\n]*\$/g, " ")
      .replace(/https?:\/\/\S+|www\.\S+/gi, " ")
      .replace(/^\s{0,3}#{1,6}\s+/gm, "")
      .replace(/^\s*[-*+]\s+/gm, "")
      .replace(/^\s*\|.*\|\s*$/gm, "")
      .replace(/(\*\*|__|\*|_|~~)/g, "")
      .replace(
        /[\p{Extended_Pictographic}\u{1F1E6}-\u{1F1FF}\u{1F3FB}-\u{1F3FF}\u{FE0F}\u{200D}\u{20E3}]/gu,
        " ",
      )
      .replace(/\s+/g, " ")
      .trim();
  }

  function showPill(label) {
    hidePill();
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

  function hidePill(immediate) {
    const pill = pillEl;
    pillEl = null;
    if (!pill || pill.parentNode !== chat) return;
    if (REDUCE || immediate) {
      pill.remove();
      return;
    }
    pill.getAnimations().forEach((anim) => {
      anim.cancel();
    });
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

  /* A still-waiting narration is superseded (new narrated reply) or
     cancelled (reset). When superseded its text is revealed immediately
     so a message never stays hidden behind a newer request. */
  function cancelPendingNarration(reveal) {
    if (!pendingNarration) return;
    const pending = pendingNarration;
    pendingNarration = null;
    pending.cancel();
    if (reveal) pending.revealNow();
  }

  /* Narrated reply flow. The reply is synthesized as ordered sentence
     chunks (SpeechChunker) with a small dispatch window of in-flight
     requests; chunks play in order, the first one the moment its audio
     is ready, so a long reply starts speaking in seconds instead of
     after one long synthesis. The response text stays hidden behind the
     "Tuning up, please wait..." pill until the first of: the first
     chunk's audio arriving (playback starts), the first chunk failing,
     or the reveal timeout elapsing — at which point in-flight synthesis
     is aborted and dropped. A chunk that errors, is refused by the
     model, or fails to decode is skipped with an event row while the
     rest of the narration continues. */
  function createNarration(chunks, reveal) {
    const controller = new AbortController();
    const audioUrls = new Array(chunks.length).fill(undefined);
    let cursor = 0;
    let inFlight = 0;
    let playIndex = 0;
    let playing = false;
    let revealed = false;
    let timer = 0;

    /* Every closure below checks `pendingNarration !== narration` so a
       superseded, finished, or cancelled attempt never touches state. */
    const narration = {
      chunks: chunks,
      controller: controller,
      dispatch: dispatch,
      cancel: cancel,
      revealNow: revealNow,
    };

    function revokeUnplayed() {
      for (let i = playIndex; i < audioUrls.length; i++) {
        if (audioUrls[i]) {
          URL.revokeObjectURL(audioUrls[i]);
          audioUrls[i] = null;
        }
      }
    }

    function revealNow() {
      if (revealed) return;
      revealed = true;
      clearTimeout(timer);
      hidePill(true);
      reveal();
    }

    /* Drop the whole attempt: abort in-flight synthesis, discard every
       not-yet-played audio, and reveal the text. Used on the reveal
       timeout, when nothing arrived while the text was hidden. */
    function drop() {
      pendingNarration = null;
      controller.abort();
      revokeUnplayed();
      revealNow();
    }

    function finishIfDrained() {
      if (!playing && inFlight === 0 && playIndex >= chunks.length) {
        pendingNarration = null;
        voiceBtn.classList.remove("speaking");
        setSpeaking(false);
      }
    }

    function startPlayback(url) {
      playing = true;
      playbackUrl = url;
      playback = new Audio(url);
      playback.onended = () => {
        URL.revokeObjectURL(url);
        playing = false;
        if (pendingNarration !== narration) return;
        playNext();
        finishIfDrained();
      };
      playback.onerror = () => {
        URL.revokeObjectURL(url);
        playing = false;
        if (pendingNarration !== narration) return;
        addEventRow(
          "Speech playback failed: audio could not be decoded.",
          true,
        );
        playNext();
        finishIfDrained();
      };
      voiceBtn.classList.add("speaking");
      setSpeaking(true);
      playback.play().catch((err) => {
        if (err.name === "AbortError") return;
        URL.revokeObjectURL(url);
        playing = false;
        voiceBtn.classList.remove("speaking");
        setSpeaking(false);
        if (pendingNarration !== narration) return;
        console.error(err);
        addEventRow("Speech playback failed: " + err.message, true);
        if (err.name === "NotAllowedError") {
          drop();
          return;
        }
        playNext();
        finishIfDrained();
      });
    }

    /* Play the next chunk in order, skipping failed ones; a chunk whose
       audio has not arrived yet pauses the queue until it does. */
    function playNext() {
      if (playing) return;
      while (playIndex < chunks.length) {
        const url = audioUrls[playIndex];
        if (url === undefined) return;
        playIndex++;
        if (url === null) continue;
        startPlayback(url);
        return;
      }
      finishIfDrained();
    }

    function dispatch() {
      while (cursor < chunks.length && inFlight < NARRATION_MAX_IN_FLIGHT) {
        inFlight++;
        synthesize(cursor++);
      }
    }

    async function synthesize(index) {
      try {
        const response = await fetch(API_BASE + "/api/speech/synthesize", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Syna-Token": window.SYNA_CONFIG?.auth_token || "",
          },
          body: JSON.stringify({ text: chunks[index] }),
          signal: controller.signal,
        });
        const payload = response.ok
          ? null
          : await response.json().catch(() => null);
        if (!response.ok) {
          throw new Error(
            (payload && payload.detail) || "HTTP " + response.status,
          );
        }
        const url = URL.createObjectURL(await response.blob());
        if (pendingNarration !== narration) {
          URL.revokeObjectURL(url);
          return;
        }
        inFlight--;
        audioUrls[index] = url;
        if (index === 0) revealNow();
        playNext();
        dispatch();
      } catch (err) {
        if (pendingNarration !== narration || controller.signal.aborted) return;
        inFlight--;
        audioUrls[index] = null;
        console.error(err);
        addEventRow("Speech synthesis failed: " + err.message, true);
        if (index === 0) revealNow();
        playNext();
        dispatch();
        finishIfDrained();
      }
    }

    function cancel() {
      controller.abort();
      clearTimeout(timer);
      playing = false;
      revokeUnplayed();
    }

    timer = setTimeout(() => {
      if (pendingNarration === narration) drop();
    }, NARRATION_REVEAL_TIMEOUT_MS);

    return narration;
  }

  function narrate(rawText, reveal) {
    const text = speakableText(String(rawText || ""));
    if (!enabled || !TTS_AVAILABLE || !text) {
      hidePill();
      reveal();
      return;
    }
    cancelPendingNarration(true);
    stopPlayback();
    showPill("Tuning up, please wait...");
    const chunker = window.SpeechChunker;
    const chunks = chunker ? chunker.split(text) : [text];
    if (!chunks.length) {
      hidePill();
      reveal();
      return;
    }
    const pending = createNarration(chunks, reveal);
    pendingNarration = pending;
    pending.dispatch();
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
      recorderStream.getTracks().forEach((track) => {
        track.stop();
      });
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
        headers: {
          "X-Syna-Token": window.SYNA_CONFIG?.auth_token || "",
        },
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
      cancelPendingNarration(false);
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
    stop();
  });

  return {
    narrate,
    stopPlayback,
    stop,
    reset,
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
