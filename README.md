# Syna

_Syna, the AI agent with a pod._

- Syna is an AI agent that can run commands inside of a docker container.
- It uses [bollard](https://pypi.org/project/bollard/) to manage the container's lifecycle, and [OpenRouter](https://openrouter.ai/) for model calls.

## Requirements

- Podman.
- An OpenRouter account.
- UV.

## Getting started

1. Clone this repo:

   ```bash
   git clone https://github.com/julynx/syna
   cd syna
   ```

2. Create a `.env` file inside the `syna` folder with the following content:

   ```sh
   OPENROUTER_API_KEY=your-openrouter-api-key
   MODEL=z-ai/glm-5.3-flash  # Or any one of your preference
   THINKING_EFFORT=medium    # Can be either of none|minimal|low|medium|high|xhigh|max
   ```

   Voice mode is enabled by adding speech models to the same `.env` file:

   ```sh
   STT_MODEL=google/chirp-3                    # Transcribes microphone recordings
   TTS_MODEL=google/gemini-3.8-flash-lite-tts  # Reads the answers out loud
   TTS_VOICE=Kore                              # Optional: voice of the TTS model
   SPEECH_LANGUAGE=en                          # Optional: ISO-639-1 code, English by default
   ```

3. Install `uv` and run:

   ```bash
   uv run syna
   ```

## Troubleshooting common problems

```text
Timed out connecting to \\.\pipe\docker... or similar
```

- Please install podman on your machine and run `podman machine init`.
- If the podman machine already exists and is running, try executing `podman machine stop` before launching syna again. It will detect it and start it on its own.
