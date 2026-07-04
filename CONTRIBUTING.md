# Contributing to Pylontech MQTT Integration

Thank you for your interest in contributing! This guide covers adding translations and general development workflow.

## Architecture Overview

This integration works in two parts:

1. **Docker sidecar** (`docker/main.py`) — runs alongside Home Assistant, connects to the Pylontech BMS over serial or TCP, and publishes parsed data to an MQTT broker.
2. **Home Assistant integration** (`custom_components/pylontech_mqtt/`) — subscribes to the MQTT broker and exposes the data as HA entities.

The parsing logic (`parser.py`, `structs.py`) lives inside the integration and is copied into the Docker image at build time, ensuring both sides always use identical logic.

## Development Setup

The devcontainer (`.devcontainer/`) sets up a matching venv automatically; if
you're not using it, activate your own venv first, then run:

```sh
make setup   # installs the pinned dev toolchain and a pre-commit hook
```

`make setup` runs `pre-commit install`, so `ruff check`/`ruff format`/
ShellCheck/Hadolint/actionlint (see `.pre-commit-config.yaml`) run
automatically on every commit — the same checks CI enforces in
`.github/workflows/tests.yaml`, just earlier.

| Command           | What it does                                            |
| ------------------ | -------------------------------------------------------- |
| `make test`        | Runs the fast test suite (`pytest`, e2e tests excluded)   |
| `make test-e2e`    | Runs the e2e suite (real subprocesses, real timing)       |
| `make lint`        | `ruff check` + `ruff format --check`                      |
| `make format`      | Applies `ruff format`                                     |
| `make typecheck`   | `mypy` + `pyright`                                        |
| `make clean`       | Removes cache/coverage artifacts                          |

### AI CLI config persistence

The devcontainer bind-mounts each AI CLI's (Claude Code, Codex, OpenCode,
Kilocode, GitHub CLI, Copilot CLI, Antigravity CLI) login/config from your
host so it survives container rebuilds — see the `mounts` comment in
`.devcontainer/devcontainer.json` for exactly what's mounted and why.

To override any of these for this project specifically (without touching
your global config), add the tool's own project-config file to the repo
root — each tool merges it on top of the mounted global config itself, no
devcontainer changes needed:

| Tool | Project override file |
| ---- | ---------------------- |
| Claude Code | `.claude/settings.json` (shared) or `.claude/settings.local.json` (gitignored) |
| Codex | `.codex/config.toml` |
| OpenCode | `opencode.json` / `opencode.jsonc` |
| Kilocode CLI | `.kilo/kilo.jsonc` (wins) or `kilo.jsonc` |
| Copilot CLI | `.github/mcp.json`, `.github/lsp.json`, `.github/hooks/` |
| Antigravity CLI | `.gemini/settings.json` |
| GitHub CLI | not supported — global config only |

## Adding Translations

We welcome translations to make this integration accessible to everyone!

1. **Locate the Translations**: Go to `custom_components/pylontech_mqtt/translations/`.
2. **Create your Language File**:
    - Find the English file: `en.json`.
    - Copy it and name the new file with your language's ISO 639-1 code (e.g., `es.json` for Spanish, `fr.json` for French, `de.json` for German).
3. **Translate**:
    - Open your new file (e.g., `es.json`).
    - Translate the values on the right side of the colon. **Do not change the keys** (the text on the left).

   **Example (`es.json`):**
   ```json
   {
     "config": {
       "step": {
         "user": {
           "data": {
             "mqtt_host": "Dirección del Broker"
           }
         }
       }
     }
   }
   ```

## Contributing a real-hardware transcript

The test suite's BMS stub (`scripts/pylon_stub.py`) is hand-authored from
the documented protocol — it has never been checked against what a real
US2000/3000/5000 or Pytes-branded unit actually sends. If you have real
hardware, running `python scripts/capture_transcript.py` against it and
opening a PR with the result (see `tests/fixtures/transcripts/README.md`)
is one of the most valuable things you can contribute; parser changes are
currently only ever validated against our own understanding of the
protocol.

## Submitting a Pull Request

1. Create a Pull Request with your changes.
2. If adding a translation, mention the language you are adding.
