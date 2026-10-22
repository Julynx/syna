"use strict";

/* First-run setup wizard: four steps (welcome, dependencies, provider,
   review), a closing sanity check, and a friendly error template with a
   copy button everywhere something can fail. */

const CONFIG = window.SYNA_CONFIG || {};
const ISSUES_URL = CONFIG.issues_url || "https://github.com/Julynx/syna/issues";

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

const PROVIDERS = {
  openrouter: {
    label: "OpenRouter (Recommended \u00b7 Most tested)",
    baseUrl: "https://openrouter.ai/api/v1",
    keysUrl: "https://openrouter.ai/settings/keys",
    model: "z-ai/glm-5.3-flash",
    needsKey: true,
  },
  openai: {
    label: "OpenAI",
    baseUrl: "https://api.openai.com/v1",
    keysUrl: "https://platform.openai.com/api-keys",
    model: "gpt-4o-mini",
    needsKey: true,
  },
  google: {
    label: "Google Gemini",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai/",
    keysUrl: "https://aistudio.google.com/apikey",
    model: "gemini-2.5-flash",
    needsKey: true,
  },
  ollama: {
    label: "Ollama (local)",
    baseUrl: "http://localhost:11434/v1",
    keysUrl: "",
    model: "llama3.2",
    needsKey: false,
  },
  custom: {
    label: "Custom endpoint",
    baseUrl: "",
    keysUrl: "",
    model: "",
    needsKey: true,
  },
};

let step = 0;
let detection = null;
let provider = "openrouter";
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
  const response = await fetch(withToken("/setup/api/" + path), {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
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

function friendlyError(action, errorText) {
  const box = document.createElement("div");
  box.className = "error-box";
  const summary = document.createElement("div");
  summary.innerHTML =
    "I tried to " +
    esc(action) +
    " and it failed." +
    '<br><span class="fineprint">Here is the error message:</span>';
  const pre = document.createElement("pre");
  pre.textContent = errorText || "(no output)";
  const details = document.createElement("details");
  details.appendChild(summary);
  details.appendChild(pre);
  box.appendChild(details);
  const row = document.createElement("div");
  row.style.marginTop = "8px";
  row.appendChild(copyButton(errorText || ""));
  box.appendChild(row);
  const issue = document.createElement("p");
  issue.className = "fineprint";
  issue.innerHTML =
    'We would greatly appreciate if you could take the time to <a href="' +
    ISSUES_URL +
    '" target="_blank">open a GitHub issue</a> and paste it there. It would' +
    " help us improve the app and ensure it works properly for everyone" +
    " who is excited about using it. Thank you.";
  box.appendChild(issue);
  return box;
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
      const status = para("Checking your system...");
      bodyEl.appendChild(status);
      api("detect")
        .then((result) => {
          detection = result;
          status.remove();
          renderDependencies(result);
        })
        .catch((error) => {
          status.remove();
          bodyEl.appendChild(friendlyError("check your system", error.message));
        });
    },
    async next() {
      if (depsSatisfied()) return;
      throw new Error("Install the missing dependencies to continue.");
    },
  },
  {
    title: "Choose a provider",
    render() {
      const field = document.createElement("div");
      field.className = "setup-field";
      const label = document.createElement("label");
      label.textContent = "Provider";
      const select = document.createElement("select");
      select.className = "setup-select";
      select.id = "provider-select";
      for (const [key, preset] of Object.entries(PROVIDERS)) {
        const option = document.createElement("option");
        option.value = key;
        option.textContent = preset.label;
        select.appendChild(option);
      }
      select.value = provider;
      field.appendChild(label);
      field.appendChild(select);
      bodyEl.appendChild(field);

      const urlField = document.createElement("div");
      urlField.className = "setup-field";
      urlField.id = "custom-url-field";
      urlField.style.display = "none";
      const urlLabel = document.createElement("label");
      urlLabel.textContent = "Endpoint URL";
      const urlInput = document.createElement("input");
      urlInput.className = "setup-input";
      urlInput.id = "custom-url";
      urlInput.placeholder = "https://example.com/v1";
      urlField.appendChild(urlLabel);
      urlField.appendChild(urlInput);
      bodyEl.appendChild(urlField);

      const keyField = document.createElement("div");
      keyField.className = "setup-field";
      keyField.id = "key-field";
      const keyLabel = document.createElement("label");
      keyLabel.textContent = "API key";
      const keyInput = document.createElement("input");
      keyInput.className = "setup-input";
      keyInput.id = "api-key";
      keyInput.type = "password";
      keyInput.placeholder = "Paste your API key";
      keyField.appendChild(keyLabel);
      keyField.appendChild(keyInput);
      bodyEl.appendChild(keyField);

      const hint = document.createElement("p");
      hint.className = "setup-hint";
      hint.id = "key-hint";
      bodyEl.appendChild(hint);

      const modelField = document.createElement("div");
      modelField.className = "setup-field";
      const modelLabel = document.createElement("label");
      modelLabel.textContent = "Model";
      const modelInput = document.createElement("input");
      modelInput.className = "setup-input";
      modelInput.id = "model-input";
      modelField.appendChild(modelLabel);
      modelField.appendChild(modelInput);
      bodyEl.appendChild(modelField);

      bodyEl.appendChild(
        para(
          "Your key is stored in a local .env file on this machine and is" +
            " only sent to the provider you choose.",
          "fineprint",
        ),
      );
      bodyEl.appendChild(dots());

      const sync = () => {
        provider = select.value;
        const preset = PROVIDERS[provider];
        urlField.style.display = provider === "custom" ? "" : "none";
        keyField.style.display =
          preset.needsKey || provider === "custom" ? "" : "none";
        if (preset.model) modelInput.value = preset.model;
        hint.innerHTML = preset.keysUrl
          ? '(<a href="' +
            preset.keysUrl +
            '" target="_blank">Click here</a>) to get a ' +
            esc(preset.label.split(" (")[0]) +
            " API key if you don't have one."
          : "No API key needed for local providers.";
      };
      select.addEventListener("change", sync);
      sync();
    },
    async next() {
      const apiKey = document.getElementById("api-key")?.value || "";
      const model = document.getElementById("model-input")?.value || "";
      const baseUrl = document.getElementById("custom-url")?.value || "";
      await api("config", {
        method: "POST",
        body: JSON.stringify({
          provider,
          base_url: baseUrl || null,
          api_key: apiKey,
          model,
        }),
      });
    },
  },
  {
    title: "Privacy and first contact",
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
      document.getElementById("provider-name").textContent =
        PROVIDERS[provider].label.split(" (")[0];
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
  return detection.podman.found || detection.docker.found;
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
      "Runs the Linux container",
      () => installDependency("wsl", rows),
    );
  }
  const engineFound = result.podman.found || result.docker.found;
  addRow(
    "download",
    result.podman.found,
    "Podman" + (result.podman.found ? "" : " (suggested)"),
    result.podman.detail,
    () => installDependency("podman", rows),
  );
  addRow("download", result.docker.found, "Docker", result.docker.detail, () =>
    installDependency("docker", rows),
  );
  bodyEl.appendChild(rows);

  if (!engineFound) {
    bodyEl.appendChild(
      para(
        "Syna executes most commands inside of an isolated container, but" +
          " no container engine was found on your system. Podman is" +
          " suggested because it runs rootless by default, for maximum" +
          " safety.",
      ),
    );
  }
}

function installDependency(name, container) {
  busy = true;
  const status = document.createElement("p");
  status.className = "inline-status";
  status.innerHTML =
    GLYPH("progress_activity", "spin") + " Installing " + name + "...";
  container.appendChild(status);
  api("install/" + name, { method: "POST" })
    .then((result) => {
      status.remove();
      if (result.status === "reboot_required") {
        bodyEl.appendChild(
          para(
            name.toUpperCase() +
              " was installed. Restart your computer, then run" +
              " <b>syna-setup</b> again to continue.",
          ),
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
      renderInstallFailure(name, result.log || "");
    })
    .catch((error) => renderInstallFailure(name, error.message))
    .finally(() => {
      busy = false;
    });
}

function renderInstallFailure(name, log) {
  const friendly = {
    wsl: "WSL2 is required to run containers on Windows.",
    podman:
      "Podman / Docker is required to create an isolated container for" +
      " Syna to use, for maximum safety.",
    docker:
      "Podman / Docker is required to create an isolated container for" +
      " Syna to use, for maximum safety.",
  };
  bodyEl.appendChild(
    para(friendly[name] || "A required dependency failed to install."),
  );
  bodyEl.appendChild(
    para(
      "I couldn't find " +
        name +
        " on your system, and I tried to install it for you but it failed.",
      "fineprint",
    ),
  );
  bodyEl.appendChild(friendlyError("install " + name, log));
  const link =
    (detection && detection.docs && detection.docs[name]) || ISSUES_URL;
  bodyEl.appendChild(
    para(
      'Check (<a href="' +
        link +
        '" target="_blank">this link</a>) to learn how to install ' +
        esc(name) +
        " yourself, then restart your computer to continue.",
      "fineprint",
    ),
  );
}

async function runSanity(button) {
  button.disabled = true;
  button.innerHTML = GLYPH("progress_activity", "spin") + " Checking...";
  try {
    const results = await api("sanity", { method: "POST" });
    const failed = Object.values(results).filter((entry) => !entry.ok);
    if (failed.length) {
      button.disabled = false;
      button.textContent = "Let's Go!";
      const box = friendlyError(
        "reach " + (results.endpoint.detail || "your provider"),
        Object.values(results)
          .filter((entry) => !entry.ok)
          .map((entry) => entry.detail)
          .join("\n"),
      );
      document.querySelector(".setup-body").appendChild(box);
      return;
    }
    const done = para("Everything checks out. Welcome aboard!");
    bodyEl.appendChild(done);
    button.textContent = "Start chatting";
    button.onclick = () => {
      window.location.href = "/";
    };
  } catch (error) {
    button.disabled = false;
    button.textContent = "Let's Go!";
    document
      .querySelector(".setup-body")
      .appendChild(friendlyError("run the sanity check", error.message));
  }
}

function render() {
  bodyEl.innerHTML = "";
  backBtn.disabled = step === 0 || busy;
  nextBtn.style.visibility = step === STEPS.length - 1 ? "hidden" : "visible";
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
  nextBtn.disabled = true;
  try {
    await STEPS[step].next();
    step = Math.min(step + 1, STEPS.length - 1);
    render();
  } catch (error) {
    bodyEl.appendChild(friendlyError("save your settings", error.message));
  } finally {
    busy = false;
    nextBtn.disabled = false;
  }
});

render();
