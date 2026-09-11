# syna

- Syna is an AI agent that can run commands inside of a docker container.
- It uses [bollard](https://pypi.org/project/bollard/) to manage the container's lifecycle, and [OpenRouter](https://openrouter.ai/) for model calls.

## Requirements

- An OpenRouter account.
- UV.

## Getting started

1. Clone this repo:

   ```text
   git clone https://github.com/julynx/syna
   cd syna
   ```

2. Create a `.env` file inside the `syna` folder with the following content:

   ```text
   OPENROUTER_API_KEY=your-openrouter-api-key
   ```

3. Install `uv` and run:

   ```bash
   uv run "syna/src/syna/__init__.py"
   ```

   > You can set the model in `syna/src/syna/ask.py` -> `ask_loop(model="...")`.
