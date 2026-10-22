"use strict";

/* First-run setup wizard: four steps (welcome, dependencies, provider,
   review), a closing sanity check, and friendly modal dialogs for unexpected errors. */

const CONFIG = window.SYNA_CONFIG || {};
const ISSUES_URL = CONFIG.issues_url || "https://github.com/Julynx/syna/issues";
const INITIAL = CONFIG.initial_config || {};

/* Provider presets come from the server (setup.PROVIDERS) so the wizard,
   the settings modal, and the runtime share one source of truth. Only the
   custom-endpoint entry is UI-only. */
const PROVIDERS = Object.assign({}, CONFIG.providers || {}, {
  custom: {
    name: "Custom endpoint",
    base_url: "",
    keys_url: "",
    model: "",
    stt_model: "",
    tts_model: "",
    tts_voice: "",
    image_model: "",
    needs_key: true,
  },
});

const bodyEl = document.getElementById("setup-body");
const titleEl = document.getElementById("setup-title");
const backBtn = document.getElementById("btn-back");
const nextBtn = document.getElementById("btn-next");

const GLYPH = (name, cls) =>
  '<span class="material-symbols-outlined' +
  (cls ? " " + cls : "") +
  '">' +
  name +
  "</span>";

const SPINNER_SVG =
  '<svg class="btn-spinner" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10"/></svg>';

// Persistent in-memory state preserved across Back/Next and prefilled from .env
const state = {
  provider: "openrouter",
  apiKey: INITIAL.api_key || "",
  model: INITIAL.model || "",
  baseUrl: INITIAL.base_url || "",
  sttModel: INITIAL.stt_model || "",
  ttsModel: INITIAL.tts_model || "",
  ttsVoice: INITIAL.tts_voice || "",
  imageModel: INITIAL.image_model || "",
};

let step = 0;
let detection = null;
let busy = false;

function withToken(url) {
  if (!CONFIG.auth_token) return url;
  return (
    url +
    (url.includes("?") ? "&" : "?") +
    "token=" +
    encodeURIComponent(CONFIG.auth_token)
  );
}

async function api(path, options) {
  const headers = {};
  if (options?.body) {
    headers["Content-Type"] = "application/json";
  }
  if (CONFIG.auth_token) {
    headers["X-Syna-Token"] = CONFIG.auth_token;
  }
  if (options?.headers) {
    Object.assign(headers, options.headers);
  }
  const response = await fetch(withToken("/setup/api/" + path), {
    ...options,
    headers,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(
      detail.detail || "Request failed (" + response.status + ")",
    );
  }
  return response.json();
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = String(text ?? "");
  return div.innerHTML;
}

function copyButton(text) {
  const btn = document.createElement("button");
  btn.className = "copy-btn";
  btn.innerHTML = GLYPH("content_copy") + "Copy";
  btn.addEventListener("click", () => navigator.clipboard.writeText(text));
  return btn;
}

function showModal(title, textHtml, errorDetails) {
  const existing = document.querySelector(".setup-modal-backdrop");
  if (existing) existing.remove();

  const backdrop = document.createElement("div");
  backdrop.className = "setup-modal-backdrop";

  const modal = document.createElement("div");
  modal.className = "setup-modal";

  const h3 = document.createElement("h3");
  h3.className = "setup-modal-title";
  h3.innerHTML = GLYPH("error") + esc(title || "Notice");
  modal.appendChild(h3);

  if (textHtml) {
    const p = document.createElement("p");
    p.className = "setup-modal-text";
    p.innerHTML = textHtml;
    modal.appendChild(p);
  }

  if (errorDetails) {
    const pre = document.createElement("pre");
    pre.className = "setup-modal-pre";
    pre.textContent = errorDetails;
    modal.appendChild(pre);
  }

  const issue = document.createElement("p");
  issue.className = "fineprint";
  issue.innerHTML =
    'If this looks like a bug, please <a href="' +
    ISSUES_URL +
    '" target="_blank">open a GitHub issue</a> so we can fix it. Thank you!';
  modal.appendChild(issue);

  const footer = document.createElement("div");
  footer.className = "setup-modal-footer";

  const left = document.createElement("div");
  if (errorDetails) {
    left.appendChild(copyButton(errorDetails));
  }
  footer.appendChild(left);

  const closeBtn = document.createElement("button");
  closeBtn.className = "setup-btn primary";
  closeBtn.textContent = "Dismiss";
  closeBtn.addEventListener("click", () => backdrop.remove());
  footer.appendChild(closeBtn);

  modal.appendChild(footer);
  backdrop.appendChild(modal);
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) backdrop.remove();
  });
  document.body.appendChild(backdrop);
}

function friendlyError(action, errorText) {
  showModal(
    "Action Failed",
    "I tried to " + esc(action) + " and it failed.",
    errorText,
  );
}

function dots() {
  const wrap = document.createElement("div");
  wrap.className = "step-dots";
  for (let i = 0; i < 4; i++) {
    const dot = document.createElement("span");
    if (i === step) dot.className = "on";
    wrap.appendChild(dot);
  }
  return wrap;
}

function para(html, cls) {
  const p = document.createElement("p");
  if (cls) p.className = cls;
  p.innerHTML = html;
  return p;
}

function clearFieldError(inputEl) {
  if (!inputEl) return;
  inputEl.classList.remove("error");
  const parent = inputEl.closest(".setup-field");
  if (parent) {
    parent.querySelectorAll(".field-error").forEach((el) => el.remove());
  }
}

function setFieldError(inputEl, message) {
  if (!inputEl) return;
  clearFieldError(inputEl);
  inputEl.classList.add("error");
  const parent = inputEl.closest(".setup-field");
  if (parent) {
    const err = document.createElement("p");
    err.className = "field-error";
    err.textContent = message;
    parent.appendChild(err);
  }
  inputEl.focus();
}

/* ---- Steps ---- */

const STEPS = [
  {
    title: "Welcome to Syna",
    render() {
      bodyEl.appendChild(
        para(
          "Syna is an AI agent with a sandboxed pod: it researches, codes," +
            " browses the web, edits files, and builds documents for you" +
            " \u2014 then reports back honestly.",
        ),
      );
      bodyEl.appendChild(
        para(
          "This guide takes about two minutes. We will check a few things" +
            " on your system, set up a provider and an API key, and run a" +
            " quick sanity check.",
          "fineprint",
        ),
      );
      bodyEl.appendChild(dots());
    },
    async next() {},
  },
  {
    title: "Dependencies",
    render() {
      if (detection) {
        renderDependencies(detection);
        return;
      }
      updateButtons();
      const status = para("Checking your system...");
      bodyEl.appendChild(status);
      api("detect")
        .then((result) => {
          detection = result;
          status.remove();
          updateButtons();
          renderDependencies(result);
        })
        .catch((error) => {
          status.remove();
          updateButtons();
          friendlyError("check your system", error.message);
        });
    },
    async next() {
      if (!detection) return false;
      return depsSatisfied();
    },
  },
  {
    title: "Choose a Provider",
    render() {
      const providerField = document.createElement("div");
      providerField.className = "setup-field";
      const providerHeader = document.createElement("div");
      providerHeader.className = "setup-field-header";
      const providerLabel = document.createElement("label");
      providerLabel.textContent = "Provider";
      providerHeader.appendChild(providerLabel);
      providerField.appendChild(providerHeader);

      const select = document.createElement("select");
      select.className = "setup-select";
      select.id = "provider-select";
      for (const [key, preset] of Object.entries(PROVIDERS)) {
        const option = document.createElement("option");
        option.value = key;
        option.textContent = preset.label || preset.name;
        select.appendChild(option);
      }
      select.value = state.provider || "openrouter";
      providerField.appendChild(select);
      bodyEl.appendChild(providerField);

      const urlField = document.createElement("div");
      urlField.className = "setup-field";
      urlField.id = "custom-url-field";
      urlField.style.display = "none";
      const urlHeader = document.createElement("div");
      urlHeader.className = "setup-field-header";
      const urlLabel = document.createElement("label");
      urlLabel.textContent = "Endpoint URL";
      urlHeader.appendChild(urlLabel);
      urlField.appendChild(urlHeader);
      const urlInput = document.createElement("input");
      urlInput.className = "setup-input";
      urlInput.id = "custom-url";
      urlInput.value = state.baseUrl || "";
      urlInput.placeholder = "https://example.com/v1";
      urlInput.addEventListener("input", () => {
        clearFieldError(urlInput);
        state.baseUrl = urlInput.value.trim();
      });
      urlField.appendChild(urlInput);
      bodyEl.appendChild(urlField);

      const keyField = document.createElement("div");
      keyField.className = "setup-field";
      keyField.id = "key-field";
      const keyHeader = document.createElement("div");
      keyHeader.className = "setup-field-header";
      const keyLabel = document.createElement("label");
      keyLabel.textContent = "API Key";
      keyHeader.appendChild(keyLabel);

      const keyLink = document.createElement("a");
      keyLink.className = "setup-field-link";
      keyLink.id = "key-link";
      keyLink.target = "_blank";
      keyHeader.appendChild(keyLink);

      keyField.appendChild(keyHeader);
      const keyInput = document.createElement("input");
      keyInput.className = "setup-input";
      keyInput.id = "api-key";
      keyInput.type = "password";
      keyInput.value = state.apiKey || "";
      keyInput.placeholder = "Paste your API key";
      keyInput.addEventListener("input", () => {
        clearFieldError(keyInput);
        state.apiKey = keyInput.value.trim();
      });
      keyField.appendChild(keyInput);
      bodyEl.appendChild(keyField);

      const modelField = document.createElement("div");
      modelField.className = "setup-field";
      const modelHeader = document.createElement("div");
      modelHeader.className = "setup-field-header";
      const modelLabel = document.createElement("label");
      modelLabel.textContent = "Model";
      modelHeader.appendChild(modelLabel);
      modelField.appendChild(modelHeader);
      const modelInput = document.createElement("input");
      modelInput.className = "setup-input";
      modelInput.id = "model-input";
      modelInput.value = state.model || PROVIDERS[select.value].model || "";
      modelInput.addEventListener("input", () => {
        clearFieldError(modelInput);
        state.model = modelInput.value.trim();
      });
      modelField.appendChild(modelInput);
      bodyEl.appendChild(modelField);

      // Advanced capabilities (voice & image models)
      const capSection = document.createElement("details");
      capSection.className = "setup-advanced";
      capSection.id = "advanced-section";
      const capSummary = document.createElement("summary");
      capSummary.textContent = "Optional features (voice, image generation)";
      capSection.appendChild(capSummary);

      const capBody = document.createElement("div");
      capBody.className = "setup-advanced-body";

      // STT Model
      const sttField = document.createElement("div");
      sttField.className = "setup-field";
      sttField.innerHTML =
        '<div class="setup-field-header"><label>Voice Dictation Model (STT)</label></div>';
      const sttInput = document.createElement("input");
      sttInput.className = "setup-input";
      sttInput.id = "stt-model-input";
      sttInput.value = state.sttModel || PROVIDERS[select.value].stt_model || "";
      sttInput.placeholder = "e.g. google/chirp-3";
      sttInput.addEventListener("input", () => {
        state.sttModel = sttInput.value.trim();
      });
      sttField.appendChild(sttInput);
      capBody.appendChild(sttField);

      // TTS Model
      const ttsField = document.createElement("div");
      ttsField.className = "setup-field";
      ttsField.innerHTML =
        '<div class="setup-field-header"><label>Voice Synthesis Model (TTS)</label></div>';
      const ttsInput = document.createElement("input");
      ttsInput.className = "setup-input";
      ttsInput.id = "tts-model-input";
      ttsInput.value = state.ttsModel || PROVIDERS[select.value].tts_model || "";
      ttsInput.placeholder = "e.g. google/gemini-3.8-flash-lite-tts";
      ttsInput.addEventListener("input", () => {
        state.ttsModel = ttsInput.value.trim();
      });
      ttsField.appendChild(ttsInput);
      capBody.appendChild(ttsField);

      // Image Model
      const imgField = document.createElement("div");
      imgField.className = "setup-field";
      imgField.innerHTML =
        '<div class="setup-field-header"><label>Image Generation Model</label></div>';
      const imgInput = document.createElement("input");
      imgInput.className = "setup-input";
      imgInput.id = "image-model-input";
      imgInput.value =
        state.imageModel || PROVIDERS[select.value].image_model || "";
      imgInput.placeholder = "e.g. google/gemini-3.1-flash-image";
      imgInput.addEventListener("input", () => {
        state.imageModel = imgInput.value.trim();
      });
      imgField.appendChild(imgInput);
      capBody.appendChild(imgField);

      capSection.appendChild(capBody);
      bodyEl.appendChild(capSection);

      bodyEl.appendChild(dots());

      const sync = () => {
        const curProvider = select.value;
        state.provider = curProvider;
        const preset = PROVIDERS[curProvider];
        urlField.style.display = curProvider === "custom" ? "" : "none";
        keyField.style.display =
          preset.needs_key || curProvider === "custom" ? "" : "none";
        capSection.style.display =
          curProvider === "openrouter" || curProvider === "custom"
            ? ""
            : "none";

        // Update default model if user hasn't overridden it
        if (
          !state.model ||
          Object.values(PROVIDERS).some((p) => p.model === state.model)
        ) {
          state.model = preset.model || "";
          modelInput.value = state.model;
        }

        // Update voice & image defaults if unset or matching a preset default
        if (preset.stt_model && !state.sttModel) {
          sttInput.value = preset.stt_model;
        }
        if (preset.tts_model && !state.ttsModel) {
          ttsInput.value = preset.tts_model;
        }
        if (preset.image_model && !state.imageModel) {
          imgInput.value = preset.image_model;
        }

        if (preset.keys_url) {
          keyLink.style.display = "";
          keyLink.href = preset.keys_url;
          keyLink.textContent = "Get an API key \u2197";
        } else {
          keyLink.style.display = "none";
        }
        clearFieldError(keyInput);
        clearFieldError(modelInput);
        clearFieldError(urlInput);
      };
      select.addEventListener("change", sync);
      sync();
    },
    async next() {
      const apiKeyInput = document.getElementById("api-key");
      const modelInput = document.getElementById("model-input");
      const urlInput = document.getElementById("custom-url");

      const apiKey = state.apiKey.trim();
      const model = state.model.trim();
      const baseUrl = state.baseUrl.trim();

      const preset = PROVIDERS[state.provider];
      const needsKey = preset ? preset.needs_key : true;

      if (state.provider === "custom" && !baseUrl) {
        setFieldError(urlInput, "Endpoint URL is required");
        return false;
      }
      if (needsKey && !apiKey) {
        setFieldError(apiKeyInput, "API key is required");
        return false;
      }
      if (!model) {
        setFieldError(modelInput, "Model name is required");
        return false;
      }

      await api("config", {
        method: "POST",
        body: JSON.stringify({
          provider: state.provider,
          base_url: baseUrl || null,
          api_key: apiKey,
          model,
          stt_model: state.sttModel || preset.stt_model || null,
          tts_model: state.ttsModel || preset.tts_model || null,
          tts_voice: state.ttsVoice || preset.tts_voice || null,
          image_model: state.imageModel || preset.image_model || null,
        }),
      });
      return true;
    },
  },
  {
    title: "Privacy and First Contact",
    render() {
      bodyEl.appendChild(
        para(
          "Syna does not collect your personal information or send your" +
            " messages anywhere except to the provider you specified" +
            " (<b id='provider-name'></b>). Their Terms of Service and" +
            " Privacy Policy apply.",
        ),
      );
      bodyEl.appendChild(
        para(
          "Pressing the button runs a final sanity check: the container" +
            " engine and one small request to your provider.",
          "fineprint",
        ),
      );
      bodyEl.appendChild(dots());
      const actions = document.createElement("div");
      actions.className = "setup-actions";
      const go = document.createElement("button");
      go.className = "setup-btn success";
      go.id = "lets-go";
      go.textContent = "Let's Go!";
      go.addEventListener("click", () => runSanity(go));
      actions.appendChild(go);
      bodyEl.appendChild(actions);
      document.getElementById("provider-name").textContent = (
        PROVIDERS[state.provider].label || PROVIDERS[state.provider].name
      ).split(" (")[0];
    },
    async next() {},
  },
];

function actionButton(label, onClick) {
  const btn = document.createElement("button");
  btn.className = "setup-btn ghost";
  btn.textContent = label;
  btn.addEventListener("click", onClick);
  return btn;
}

function depsSatisfied() {
  if (!detection) return false;
  const platform = detection.platform;
  if (platform === "win32" && !detection.wsl.found) return false;
  const select = document.getElementById("engine-pill-select");
  if (select && select.value === "docker") {
    return Boolean(detection.docker.found);
  }
  if (select && select.value === "podman") {
    return Boolean(detection.podman.found);
  }
  return Boolean(detection.podman.found || detection.docker.found);
}

function renderDependencies(result) {
  const platform = result.platform;
  const rows = document.createElement("div");
  rows.className = "checklist";

  const addRow = (icon, found, label, detail, install) => {
    const row = document.createElement("div");
    row.className = "row " + (found ? "ok" : "missing");
    row.innerHTML =
      GLYPH(found ? "check_circle" : icon) +
      "<span>" +
      esc(label) +
      "</span>" +
      (detail ? '<span class="detail">' + esc(detail) + "</span>" : "");
    if (!found && install) row.appendChild(actionButton("Install", install));
    rows.appendChild(row);
  };

  if (platform === "win32") {
    addRow(
      "download",
      result.wsl.found,
      "WSL2",
      result.wsl.found
        ? "Runs the Linux container"
        : "Required for containers on Windows",
      result.wsl.found ? null : () => installDependency("wsl", rows),
    );
  }

  // Unified Container Engine Dropdown Pill
  const engineRow = document.createElement("div");
  engineRow.id = "engine-pill-row";

  // Pre-selection: If Docker is installed and Podman is NOT, pre-select Docker.
  // Otherwise default to Podman (recommended/rootless).
  let selectedEngine =
    result.docker.found && !result.podman.found ? "docker" : "podman";

  const updateEngineRow = () => {
    engineRow.innerHTML = "";
    const isPodman = selectedEngine === "podman";
    const engineData = isPodman ? result.podman : result.docker;
    const isInstalled = Boolean(engineData.found);

    if (isInstalled) {
      engineRow.className = "row ok";
      engineRow.innerHTML = GLYPH("check_circle");
    } else {
      engineRow.className = "row warn";
      engineRow.innerHTML = GLYPH("download");
    }

    const select = document.createElement("select");
    select.className = "engine-pill-select";
    select.id = "engine-pill-select";

    const optPodman = document.createElement("option");
    optPodman.value = "podman";
    optPodman.textContent = "Podman (suggested)";
    select.appendChild(optPodman);

    const optDocker = document.createElement("option");
    optDocker.value = "docker";
    optDocker.textContent = "Docker";
    select.appendChild(optDocker);

    select.value = selectedEngine;
    select.addEventListener("change", (e) => {
      selectedEngine = e.target.value;
      updateEngineRow();
      updateButtons();
    });
    engineRow.appendChild(select);

    const detailSpan = document.createElement("span");
    detailSpan.className = "detail";
    detailSpan.textContent = isInstalled
      ? engineData.detail || "Ready"
      : "Not installed";
    engineRow.appendChild(detailSpan);

    if (!isInstalled) {
      const installBtn = document.createElement("button");
      installBtn.className = "setup-btn success";
      installBtn.textContent = "Install";
      installBtn.style.marginLeft = "8px";
      installBtn.addEventListener("click", () =>
        installDependency(selectedEngine, rows),
      );
      engineRow.appendChild(installBtn);
    }
  };

  updateEngineRow();
  rows.appendChild(engineRow);

  bodyEl.appendChild(rows);

  if (!result.podman.found && !result.docker.found) {
    bodyEl.appendChild(
      para(
        "Syna executes commands inside an isolated container, but" +
          " no container engine was found on your system. Podman is" +
          " suggested because it runs rootless by default.",
      ),
    );
  }
}

function installDependency(name, container) {
  busy = true;
  updateButtons();
  const status = document.createElement("p");
  status.className = "inline-status";
  status.innerHTML =
    SPINNER_SVG + "<span>Installing " + esc(name) + "...</span>";
  container.appendChild(status);
  api("install/" + name, { method: "POST" })
    .then((result) => {
      status.remove();
      if (result.status === "reboot_required") {
        showModal(
          "Restart Required",
          esc(name.toUpperCase()) +
            " was installed. Please restart your computer, then launch Syna again to continue.",
        );
        nextBtn.disabled = true;
        return;
      }
      if (result.status === "ok") {
        return api("detect").then((fresh) => {
          detection = fresh;
          container.remove();
          renderDependencies(fresh);
        });
      }
      showInstallFailure(name, result.log || "");
    })
    .catch((error) => showInstallFailure(name, error.message))
    .finally(() => {
      busy = false;
      updateButtons();
    });
}

function showInstallFailure(name, log) {
  const link =
    (detection && detection.docs && detection.docs[name]) || ISSUES_URL;
  const text =
    "Failed to install " +
    esc(name) +
    '. You can install it manually from <a href="' +
    link +
    '" target="_blank">here</a>, then restart your terminal.';
  showModal("Installation Failed", text, log);
}

async function runSanity(button) {
  button.disabled = true;
  button.innerHTML = SPINNER_SVG + "<span>Checking...</span>";
  try {
    const results = await api("sanity", { method: "POST" });
    const failed = Object.values(results).filter((entry) => !entry.ok);
    if (failed.length) {
      button.disabled = false;
      button.textContent = "Let's Go!";
      const details = Object.values(results)
        .filter((entry) => !entry.ok)
        .map((entry) => entry.detail)
        .join("\n");
      showModal(
        "Sanity Check Failed",
        "Could not verify " +
          esc(results.endpoint.detail || "your provider settings") +
          ".",
        details,
      );
      return;
    }
    const done = para("Everything checks out. Welcome aboard!");
    bodyEl.appendChild(done);
    button.disabled = false;
    button.textContent = "Start chatting";
    button.onclick = () => {
      window.location.href = "/";
    };
  } catch (error) {
    button.disabled = false;
    button.textContent = "Let's Go!";
    friendlyError("run the sanity check", error.message);
  }
}

function updateButtons() {
  backBtn.disabled = step === 0 || busy;
  nextBtn.disabled = busy || (step === 1 && (!detection || !depsSatisfied()));
  nextBtn.style.visibility = step === STEPS.length - 1 ? "hidden" : "visible";
  if (!busy) {
    nextBtn.innerHTML = "Next";
  }
}

function render() {
  bodyEl.innerHTML = "";
  if (titleEl && STEPS[step]) {
    titleEl.textContent = STEPS[step].title;
  }
  updateButtons();
  STEPS[step].render();
}

backBtn.addEventListener("click", () => {
  if (step > 0 && !busy) {
    step--;
    render();
  }
});

nextBtn.addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  nextBtn.innerHTML = SPINNER_SVG + "<span>Next</span>";
  updateButtons();
  try {
    const ok = await STEPS[step].next();
    if (ok === false) {
      busy = false;
      updateButtons();
      return;
    }
    step = Math.min(step + 1, STEPS.length - 1);
    busy = false;
    render();
  } catch (error) {
    busy = false;
    updateButtons();
    friendlyError("save your settings", error.message);
  } finally {
    busy = false;
    updateButtons();
  }
});

render();
