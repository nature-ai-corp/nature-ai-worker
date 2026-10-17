# Nature AI Worker v0.1.0

**Connect your model. Configure. Start working.**

The first public release of Nature AI Worker: a lightweight, self-hosted, general-purpose AI agent that runs on your own computer. Connect the model you already use, put files in a workspace folder, and describe tasks in plain language.

> Early release. Single user, local use only. Read **Security precautions** before installing.

## Core features

- **One built-in General Agent.** No agents, workflows or code to set up. It plans, calls tools step by step, and reports what it did.
- **Workspace tools:** list, read, search, write and delete files; summarize CSV, TSV and JSON tables; exact arithmetic; optional web fetch (off by default).
- **Approval before risky actions.** Overwriting or deleting a file and fetching a web page always wait for you to click Approve. This is enforced where tools run, not by the prompt.
- **Workspace confinement.** The agent can only access one folder. Path traversal, absolute paths and symlinks pointing outside are blocked.
- **Many providers:** OpenAI, Anthropic Claude, Google Gemini, DeepSeek, Ollama (local models), and any OpenAI-compatible API.
- **Encrypted key storage.** API keys are encrypted at rest, never returned by the API, never written to history, redacted from logs, and only sent to the endpoint they were saved for.
- **Small footprint:** one Python process, SQLite, and a plain web UI. Conversations are kept across restarts.

## Installation

Python 3.11–3.14:

```bash
git clone https://github.com/nature-ai-corp/nature-ai-worker.git
cd nature-ai-worker
python3 -m venv .venv && source .venv/bin/activate
pip install .
nature-worker            # then open http://127.0.0.1:8080
```

Docker Compose (v2.24 or later):

```bash
git clone https://github.com/nature-ai-corp/nature-ai-worker.git
cd nature-ai-worker
mkdir -p data workspace
NATURE_WORKER_UID=$(id -u) NATURE_WORKER_GID=$(id -g) docker compose up -d --build
```

On first start, choose a provider, enter a model that supports tool calling, click **Test connection**, then **Save**. See the README for details.

## Verified providers

| Provider | Status |
|---|---|
| OpenAI | Verified with real calls (`gpt-4.1-mini`) |
| Ollama | Verified with real calls (`qwen3.8:27b`) |
| Anthropic, Google Gemini, DeepSeek | Supported, but **not yet verified against the real services**. Tested against a local fake server using the real SDKs. Reports welcome. |
| Other OpenAI-compatible servers | Supported if the server implements the `tools` parameter; no specific server verified |

The test suite passes in CI on Python 3.11, 3.12, 3.13 and 3.14. CI also builds the Docker image and checks health, the workspace mount, file permissions and persistence. Evidence: [docs/VERIFICATION.md](https://github.com/nature-ai-corp/nature-ai-worker/blob/main/docs/VERIFICATION.md).

## Known limitations

- Single user, no login. Intended for `127.0.0.1` only.
- Linux first: tested on Ubuntu 24.04 and WSL2. macOS and native Windows are untested; on Windows, use WSL2 or Docker.
- Text files only (UTF-8 text, CSV/TSV, JSON). No PDF, Word, Excel or images.
- No streaming, and no context management: very long conversations eventually exceed the model's context window.
- Tasks are not resumed if the server stops mid-task.
- Results depend on the model's tool-calling ability. Small local models may call tools poorly.
- Web fetch returns text only and does not run JavaScript.

## Security precautions

- **Do not expose the port to a network.** Anyone who can reach it can use the agent, read the workspace and use your provider account. Keep the default `127.0.0.1` binding, or put an authenticating reverse proxy in front.
- **Only put files in the workspace that you are willing to send to your AI provider.** The model can read every file there.
- **Set `NATURE_WORKER_SECRET_KEY`** so that a copy of the data folder alone does not reveal your stored keys.
- **Review each approval request**, including web URLs, before approving. File and web content can contain prompt-injection attempts; approval is the control for destructive or outbound actions.
- Report vulnerabilities privately as described in [SECURITY.md](https://github.com/nature-ai-corp/nature-ai-worker/blob/main/SECURITY.md), not in public issues.

Licensed under Apache-2.0. Third-party dependencies and their licenses are listed in `THIRD_PARTY.md`.
