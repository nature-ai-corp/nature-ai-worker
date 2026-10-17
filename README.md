# Nature AI Worker

**Connect your model. Configure. Start working.**

Nature AI Worker is a lightweight, self-hosted, general-purpose AI agent. Run it on your own computer, connect the AI model you already use, and give it tasks in plain language. It can read, search and write files in a workspace folder, summarize data, do exact calculations, and (optionally) fetch web pages.

You do not need to write code, create agents, or design workflows. There is one built-in General Agent.

- One Python process, SQLite storage, and a small web UI. No other services are needed.
- Works with OpenAI, Anthropic Claude, Google Gemini, DeepSeek, Ollama (local models), and any OpenAI-compatible API.
- File access is limited to one workspace folder. Overwriting or deleting files and fetching web pages need your approval.
- Licensed under Apache-2.0.

> Status: v0.1, an early release. See [Known limitations](#known-limitations).

---

## Installation

### Option A: Python (3.11+; tested on 3.14)

```bash
git clone https://github.com/nature-ai-corp/nature-ai-worker.git
cd nature-ai-worker
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install .
nature-worker
```

Open **http://127.0.0.1:8080** in your browser.

By default, data is stored in `./data` and the workspace folder is `./workspace`, both relative to the folder you start from.

### Option B: Docker Compose

```bash
git clone https://github.com/nature-ai-corp/nature-ai-worker.git
cd nature-ai-worker
mkdir -p data workspace            # create these first so they are owned by you
NATURE_WORKER_UID=$(id -u) NATURE_WORKER_GID=$(id -g) docker compose up -d --build
```

Requires Docker Compose v2.24 or later (for the optional `.env` file). Without `NATURE_WORKER_UID`/`NATURE_WORKER_GID`, the container runs as user 1000. If your user ID differs, `./data` must be writable by that user.

Open **http://127.0.0.1:8080**. Put files for the agent in `./workspace`.

The port is published on `127.0.0.1` only, so other machines cannot reach it.

---

## First-run setup

The first time you open the app, a setup dialog asks for:

| Field | What to enter |
|---|---|
| **Provider** | OpenAI, Anthropic Claude, Google Gemini, DeepSeek, Ollama (local), or OpenAI-compatible API |
| **Model** | The model name, e.g. `gpt-5-mini`, `claude-sonnet-5-5`, `gemini-2.5-flash`, `deepseek-chat`, `qwen3:8b`. These are examples only; check your provider's current model list. The model must support **tool calling**. |
| **API key** | Required for cloud providers. Optional for Ollama and OpenAI-compatible servers. |
| **Endpoint URL** | Ollama: `http://localhost:11434` (from Docker: `http://host.docker.internal:11434`). OpenAI-compatible: the base URL including `/v1`, e.g. `http://localhost:1234/v1`. |

Click **Test connection** first. It makes one small real request and checks that the model can call a tool. Then click **Save**.

You can change these later under **Settings**. Settings also includes:

- **Allow web fetching**: off by default. When it is on, the agent can request public web pages, and you approve each request.
- **Max steps per task**: default 20. This is how many model calls one task may use before it stops and asks you to continue.

### Provider notes

| Provider | Notes |
|---|---|
| OpenAI | Uses the Chat Completions API. Key from platform.openai.com. |
| Anthropic | Uses the Messages API. Key from console.anthropic.com. |
| Google Gemini | Uses the Gemini API (`google-genai`). Key from aistudio.google.com. |
| DeepSeek | Uses `https://api.deepseek.com` (OpenAI-compatible). `deepseek-chat` supports tools. |
| Ollama | Run `ollama pull <model>` first. Use a model that supports tools, such as `qwen3`, `llama3.1`, or `mistral-nemo`. Models without tool support produce a clear error. |
| OpenAI-compatible | LM Studio, vLLM, LiteLLM, OpenRouter and similar. The server must support the `tools` parameter. |

---

## Using it

1. Put the files you want to work with in the workspace folder. You can also use **Workspace files → Upload file**.
2. Describe a task in the chat, for example:
   - *"Read sales.csv and write a report.md with totals per region."*
   - *"Search my notes for anything about invoices and summarize it."*
   - *"Turn meeting-notes.txt into a list of action items in actions.md."*
3. Tool calls appear in the chat as they happen; click a tool card to see its details. When the agent wants to overwrite or delete a file or fetch a web page, an **Approval needed** card appears. Nothing happens until you click **Approve** or **Deny**.
4. Results are saved in the workspace. You can preview and download them from **Workspace files**.

Conversations are saved and listed in the sidebar. They are kept after restarts.

### Built-in tools

| Tool | What it does | Approval |
|---|---|---|
| `list_files` | List files and folders in the workspace | No |
| `read_file` | Read a text file (long files are read in chunks) | No |
| `search_text` | Find text or a regex across workspace files | No |
| `table_summary` | Row and column statistics for CSV, TSV or JSON tables | No |
| `calculate` | Exact arithmetic (safe expression evaluator, no code execution) | No |
| `write_file` | Create a file | No for new files; **yes to overwrite** |
| `delete_file` | Delete a file | **Always** |
| `web_fetch` | Fetch a public web page as text (only when enabled in Settings) | **Always** |

There is no shell or Python execution tool.

---

## Configuration reference

All settings are optional environment variables (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `NATURE_WORKER_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` inside Docker) |
| `NATURE_WORKER_PORT` | `8080` | Port |
| `NATURE_WORKER_DATA_DIR` | `./data` | SQLite database and encrypted credentials |
| `NATURE_WORKER_WORKSPACE` | `./workspace` | The only folder the agent can access |
| `NATURE_WORKER_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Accepted `Host` headers (DNS-rebinding protection) |
| `NATURE_WORKER_SECRET_KEY` | *(generated)* | Passphrase that encrypts saved API keys |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `OPENAI_COMPATIBLE_API_KEY` | | Provider keys. If set, these override keys saved in the UI. `OPENAI_COMPATIBLE_API_KEY` is sent to whichever OpenAI-compatible endpoint is set in Settings. |

### How API keys are stored

- Keys you enter in the UI are encrypted (Fernet: AES-128-CBC with HMAC-SHA256) in `data/credentials.enc`, which has `0600` permissions.
- The encryption key is `NATURE_WORKER_SECRET_KEY` if you set it. Otherwise it is a random key in `data/master.key` (also `0600`). With the generated key, the protection comes down to file permissions. Set `NATURE_WORKER_SECRET_KEY` so that a copy of the data folder alone does not reveal your keys.
- Keys are never stored in SQLite, never returned by the API (the UI only shows whether a key is set), never stored in conversation history, and are redacted from logs.
- Alternatively, set keys only as environment variables, and nothing is written to disk.
- **Keys only go where you configured them.** OpenAI, Anthropic, Gemini and DeepSeek keys are only sent to those providers' official endpoints. A key saved for an Ollama or OpenAI-compatible endpoint is bound to that exact endpoint. If you change the endpoint, the key is not sent to the new address until you enter it again.
- SDK environment overrides are ignored (a warning at startup names any that are set): `OPENAI_BASE_URL`, `OPENAI_CUSTOM_HEADERS`, `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, `OPENAI_ADMIN_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`, `ANTHROPIC_AUTH_TOKEN`, `GOOGLE_GEMINI_BASE_URL`, `GOOGLE_VERTEX_BASE_URL`, `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_GENAI_USE_ENTERPRISE`, `GOOGLE_API_KEY`. To use a proxy or gateway, choose **OpenAI-compatible API** and enter its URL in Settings.

---

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The tests run the real application, including the real provider SDKs, against a local fake model server that speaks each provider's wire format. They also start the server as a subprocess to check that data survives a restart. They do **not** contact real AI providers.

To verify against a **real** provider, run the acceptance script. It starts the server in a temporary folder and runs the acceptance scenarios through the HTTP API:

```bash
# Put the key in a private file from your own terminal (never on the command line or in a chat):
install -m 600 /dev/null ~/.nature-worker-openai-key && nano ~/.nature-worker-openai-key
.venv/bin/python scripts/acceptance.py --provider openai --model <model> --key-file ~/.nature-worker-openai-key
.venv/bin/python scripts/acceptance.py --provider ollama --model <pulled-model> --endpoint http://localhost:11434
```

Step-by-step instructions, including Ollama on another computer, are in [docs/REAL_TESTING.md](docs/REAL_TESTING.md). Results are recorded in [docs/VERIFICATION.md](docs/VERIFICATION.md).

If the service, model or key is unavailable, the script reports **NOT EXECUTED** (exit code 2) instead of failing.

It writes a Markdown report (`acceptance-<provider>.md`) containing transcripts of each scenario.

### Project layout

```
src/nature_worker/
  app.py            FastAPI routes, security middleware
  agent.py          The General Agent: bounded model/tool loop, approval pause/resume
  db.py             SQLite: conversations, messages, approvals, settings
  credentials.py    Encrypted API key storage, log redaction
  config.py         Environment configuration
  providers/        One adapter per API (OpenAI-compatible, Anthropic, Gemini, Ollama)
  tools/            Workspace sandbox, file tools, data tools, guarded web fetch
  static/           The web UI (plain HTML/CSS/JS, no build step)
tests/              Automated tests
scripts/acceptance.py   Real-provider acceptance check
```

---

## Known limitations

- **Single user, local use.** There is no login. The server listens on localhost only. Do not expose it to a network without putting an authenticating reverse proxy in front.
- **Text files only.** The agent reads UTF-8 text, CSV/TSV and JSON. PDF, Word, Excel and images are not supported.
- **No streaming.** Replies appear when each step finishes. The UI polls for progress every second.
- **No context management.** The whole conversation is sent to the model on every step. Very long conversations eventually exceed the model's context window. The app reports this clearly; start a new conversation.
- **One run at a time per conversation.** Runs happen inside the HTTP request. If the server stops in the middle of a task, the task is not resumed. Unanswered tool calls are closed out the next time you send a message.
- **Tool-calling quality depends on the model.** Small local models may call tools poorly. Use **Test connection** to check.
- The web fetch tool returns text only (HTML, plain text, JSON, XML). It does not run JavaScript.
- Tested on Linux with Python 3.14. macOS and Windows have not been tested yet.

## License

Apache License 2.0. See [LICENSE](LICENSE). The third-party dependencies and their licenses are listed in [THIRD_PARTY.md](THIRD_PARTY.md).
