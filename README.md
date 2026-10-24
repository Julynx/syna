<p align="center">
  <img
    src="https://raw.githubusercontent.com/Julynx/syna/main/website/assets/syna-logo-pill.png"
    width="260px"
  />
</p>

<h3 align="center">Syna</h3>
<p align="center">
  <i>The AI agent with a pod.</i>
  <br />
  <a href="https://julynx.github.io/syna/" target="_blank">julynx.github.io/syna</a>
</p>

<hr />

<p align="center">
  <h3 align="center"><code>uv tool install syna-agent</code></h3>
</p>

![Syna chat interface](https://raw.githubusercontent.com/Julynx/syna/main/website/assets/screenshot.png)

## You're getting the full kit

> **Syna has a pod,** a Debian container, and her commands run inside of it. They cannot touch your actual system. She can only see what you decide to share with her. She respects your boundaries.

> **Syna has a browser,** and native web search and web interaction tools, no API key required.

> **Syna has a voice;** enable voice mode to talk to Syna and listen to her. She can transcribe an audio file into text, and the other way around, powered by the models of your choice.

> **Syna has sight;** she can understand images and ensure the documents, graphs and websites she creates look amazing before sharing them with you. Say goodbye to troubleshooting broken layouts.

> **Syna has a canvas,** and your favorite model is the brush. Ask her to paint something for you, and she will take care of the rest.

> **Syna has principles.** She knows how to do her research, supports her answers with sources, and verifies her own work.

> **Syna has a pen,** and her notebooks have an index. Long files don't scare her, she can locate the exact line that needs to change, and replace it without having to read the entire file and spit it out like a maniac.

> **Syna has a control panel,** and it's a gift for you. Customize everything. Don't want voice mode or image generation? Disable them, they'll stay off.

> **Syna has patience.** She is ready for long and complex tasks. When her workspace gets cluttered, she doesn't panic, she tidies up. She uses elision, compaction, and caching to keep your bills low and make every token count.

> **Syna has connections.** OpenRouter is her best friend, but she also works with OpenAI, Google Gemini, local Ollama, or any OpenAI-compatible endpoint. Pick your favorite.

> **Syna is a star,** and she looks like one. She pulses in the background as she works, changes colors, and thinks in polytones. She knows the agent of the future shouldn't look like a gray chat.

## Getting started

### Using Syna (Web UI) - Recommended

1. Open your `Terminal` and paste the following command to install Syna:

   ```bash
   uv tool install syna-agent
   ```

   > The `uv` package manager is the recommended way to install Syna.
   > If you don't have it installed, run the following command depending on your system:
   >
   > - Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   > - Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`

2. Syna is now installed. Enter the following command into your `Terminal` to run it:

   ```bash
   syna
   ```

   Then, open your browser and go to the following url: `http://127.0.0.1:8000`

---

### Using Syna (CLI)

1. Before you start, run the following command:

   ```bash
   syna-setup
   ```

   A wizard will guide you into configuring your API key, model, and other settings.
   It saves them into an `.env` file in `~/.syna`.

   <details>
     <summary>If you want to do this manually: [Click me]</summary>

     Create a new file in `~/.syna/.env` with the following contents:

     ```sh
     OPENROUTER_API_KEY=your-api-key         # Your API key goes here
     MODEL=~z-ai/glm-flash-latest            # Or any model of your preference
     BASE_URL=https://openrouter.ai/api/v1   # Optional: Any OpenAI-compatible endpoint
     THINKING_EFFORT=medium                  # Can be either of none|minimal|low|medium|high|xhigh|max
     ```

     Voice mode is enabled by adding speech models to the same `.env` file:

     ```sh
     STT_MODEL=google/chirp-3                    # Transcribes microphone recordings
     TTS_MODEL=google/gemini-3.8-flash-lite-tts  # Reads the answers out loud
     TTS_VOICE=Kore                              # Optional: voice of the TTS model
     SPEECH_LANGUAGE=en                          # Optional: ISO-639-1 code, English by default
     ```

     Image generation (the model's `text_to_image` tool) is enabled the same way:

     ```sh
     IMAGE_MODEL=google/gemini-3.1-flash-image
     ```

     > Please note that you need to have either `Podman` or `Docker` installed on your system.
     > The setup wizard can install it for you in one click, or you can also do so manually.
     > Podman is the recommended option, visit [podman.io](https://podman.io/) to get it.
   </details>

2. You're good to go. All it takes is:

   ```bash
   syna-cli
   ```

## Privacy

Syna does not collect your personal information or send your messages anywhere except to the provider you specified.
Their _Terms of Service_ and _Privacy Policy_ apply.

## Links

- [Website](https://julynx.github.io/syna/)
- [PyPI](https://pypi.org/project/syna-agent/)
- [Report an issue](https://github.com/Julynx/syna/issues)
