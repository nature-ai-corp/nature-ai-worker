# v0.1 Verification Record

> **Current status (v0.1.0, 2026-10-17):** tests pass on Python 3.11–3.14 and the Docker deployment passes its runtime checks, both in GitHub Actions. Real-provider acceptance passed for OpenAI (`gpt-4.1-mini`) and Ollama (`qwen3.8:27b`). Anthropic, Gemini and DeepSeek are not yet verified against their real services. See the [final section](#wkr-016--wkr-017-ci-docker-and-release-readiness-2026-10-17). The sections below are kept in date order as the record of how this was reached; earlier "NOT EXECUTED" entries were superseded by later ones.

Date: 2026-10-17. Environment: Linux (WSL2), Python 3.14.4, SQLite 3.46.1, Chrome (headless).
No Docker, no Ollama, and no provider API keys were available in this environment.

## Summary of acceptance tests

| # | Acceptance test | Result | Evidence |
|---|---|---|---|
| A | Clean installation | **PASS (pip)** / **NOT EXECUTED (Docker)** | `pip install .` into a fresh venv succeeded. The server started and served the UI and API. Docker is not installed here, so the Dockerfile and compose.yaml have **not been built or run**. |
| B | Cloud provider + real tool-calling task | **NOT EXECUTED** | No API keys available. The adapters for OpenAI, Anthropic, Gemini and DeepSeek were tested only against a local fake server that speaks each API's wire format, using the real SDKs. That is **not** verification against the real services. |
| C | Ollama + tool-capable local model | **NOT EXECUTED** | Ollama is not installed. The adapter was tested only against a fake `/api/chat` server. |
| D | Agent reads files, generates report.md | **PASS (scripted model)** | `test_agent_reads_workspace_and_writes_report`, the UI check (screenshot 03), and `test_history_persists_across_restart` (real server process). The model's decisions were scripted. Real model behavior has not been verified (see B/C). |
| E | Workspace escape attempts blocked | **PASS** | 10 traversal/absolute/symlink read cases, 4 write cases, listing hides escaping symlinks, file API rejects escapes, and an agent run attempting escapes leaks nothing. |
| F | Sensitive operations need approval | **PASS** | Execution-layer refusal without approval; overwrite/delete pause the run; deny leaves files untouched; approvals are single-use; a new message cancels pending approvals; UI approval flow in headless Chrome (screenshot 05). |
| G | Provider failures reported gracefully | **PASS (simulated failures)** | HTTP 401/404/429/400-context/500, connection refused, and Ollama "does not support tools". Each produces a plain-language notice with the provider's own message (screenshot 07). |
| H | History persists after restart | **PASS** | The real server process was stopped with SIGTERM and restarted. Conversations, messages, settings and the encrypted key survived. |
| I | Runs without NATURE AI Platform | **PASS** | Import scan: only stdlib plus declared dependencies; no Platform package, service or database is referenced. All tests ran with no other local services. |

## Automated tests

`pytest`: **85 passed** in about 20 s (0 failed, 0 skipped). This includes one live fetch of `https://example.com`.

| File | Covers |
|---|---|
| `tests/test_agent.py` | Multi-step tool loop, report generation, step limit, tool errors, invalid tool arguments, unconfigured app, duplicate tool-call ids |
| `tests/test_security.py` | Workspace confinement (E), approvals (F), CSRF and Host checks, key storage and redaction |
| `tests/test_providers.py` | Error mapping (G), connection test, and wire format for the Anthropic, Gemini (thought signatures), Ollama and DeepSeek (reasoning_content) adapters |
| `tests/test_tools.py` | Data tools, safe calculator, chunked reads, SSRF protection (private IPs, localhost, DNS rebinding, redirect to a private address) |
| `tests/test_process.py` | Real server subprocess: restart persistence (H), key absent from logs, dependency scan (I) |

## UI verification

Headless Chrome drove the real server (with the scripted fake model) through: first-run setup → Test connection → Save → run a task → expand tool details → overwrite approval → Approve (file changed only after approval) → provider error → Workspace panel and preview → reload (history kept) → mobile width. Screenshots are in `docs/screenshots/`.

This check found and fixed four defects: an endpoint field shown for providers that do not need it, a delete icon that rendered as an empty box, a duplicate approval card when a server reuses tool-call ids, and raw dict text in provider error messages.

## How to complete B and C

See "Real-provider test commands" at the end of this document. (The earlier `export MY_KEY=...` form was replaced because it leaves the key in shell history.)

---

# WKR-007 Release Readiness Review (2026-10-17)

**Verdict: NOT release-ready.** No real model has been tested, and Docker has not been run.

| Check | Result |
|---|---|
| Automated tests | **87 passed** (85 earlier + 2 regression tests added in this review). Also 85/85 against a clean, non-editable install. |
| A. Install (pip, Python 3.14) | PASS |
| A. Install (Docker) | **NOT EXECUTED**: no Docker engine. Static review only. |
| Python 3.11 / 3.12 / 3.13 | Grammar parses and all dependencies declare support. Runtime **NOT EXECUTED** (no interpreters available). |
| B. Cloud provider, real calls | **NOT EXECUTED**: no API key supplied |
| C. Ollama, real calls | **NOT EXECUTED**: no Ollama on localhost or on the WSL host gateway |
| D, E, F, G, H (scripted model) | PASS: tests, UI check in headless Chrome, and acceptance-script logic run against a fake server |
| I. No Platform dependencies | PASS |
| Scope (no work loop, recovery, scheduler, enterprise) | PASS |
| License audit | PASS: 39 runtime packages, all permissive (certifi MPL-2.0, unmodified) |

Defects found and fixed in this review:
1. API keys inside exception tracebacks were not redacted from logs (security).
2. A dangling symlink in the workspace crashed file listing (`/api/files` returned 500).
3. The acceptance script had no safe way to supply a key: `--key-file` was added (refuses world-readable files), plus a preflight that reports NOT EXECUTED.
4. The acceptance G check gave a false failure for OpenAI-compatible servers.
5. Docs: compose UID behaviour (the `UID=` form later proved broken in bash and was replaced in WKR-008), the Compose ≥ 2.24 requirement, model names marked as examples, and three residual risks added to SECURITY.md.

---

# WKR-008 Security Hardening (2026-10-17)

**Verdict: still NOT release-ready.** Real-provider tests (B, C) and Docker remain NOT EXECUTED.

## Security fix: provider endpoint and credential routing

**Problem.** The provider SDKs read environment variables in their constructors whenever a value is not passed explicitly:
- **Redirect requests:** `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`, `GOOGLE_GEMINI_BASE_URL`, `GOOGLE_VERTEX_BASE_URL`, `GOOGLE_GENAI_USE_VERTEXAI` / `_ENTERPRISE`.
- **Inject headers or credentials:** `OPENAI_CUSTOM_HEADERS` (including `Authorization`), `ANTHROPIC_CUSTOM_HEADERS`, `ANTHROPIC_AUTH_TOKEN`, `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, `OPENAI_ADMIN_KEY`, `GOOGLE_API_KEY`.

Since the OpenAI-compatible adapter uses the OpenAI SDK, an ambient `OPENAI_CUSTOM_HEADERS` token would have been sent to any user-entered endpoint. Separately, a key saved for an OpenAI-compatible or Ollama endpoint was sent to a new endpoint if the user changed the URL, including from "Test connection". Provider error text, which a hostile endpoint can make echo the key, was stored in chat history.

**Fix** (`providers/__init__.py`, the adapter constructors, `credentials.py`, `app.py`, `static/app.js`, `__main__.py`):
1. Official endpoints are always passed explicitly: `https://api.openai.com/v1`, `https://api.anthropic.com`, `https://generativelanguage.googleapis.com/`, `https://api.deepseek.com`. Gemini also gets `vertexai=False`.
2. `isolated_sdk_env()` hides the variables above while SDK clients are constructed, under a lock, then restores them. The process environment is unchanged afterwards. At startup, a warning names (never prints) any of these variables that are set.
3. Keys saved for configurable endpoints (OpenAI-compatible, Ollama) are stored with their normalized endpoint and only used for that endpoint. On a mismatch, the key source is `other_endpoint` and the UI asks for the key again. User-configured compatible endpoints work as before.
4. A wrapper redacts the active key from provider error messages and replies before anything is stored or returned.

## Evidence

| Check | Result |
|---|---|
| Automated tests | **99 passed** (87 before + 12 in `tests/test_routing.py`) |
| Routing tests | Base-URL env cannot redirect openai/anthropic/gemini; official endpoints are explicit defaults (incl. deepseek, Vertex off); a user-configured compatible endpoint is kept even with `OPENAI_BASE_URL` set; custom-header, org/project and ambient-token env are not injected (OpenAI and Anthropic); Gemini ignores `GOOGLE_API_KEY` and the Vertex switch; stored key not sent after an endpoint change (chat and Test connection), and used again after switching back (normalized); Ollama key bound to its endpoint; key echoed by an endpoint is redacted from history, the DB file and the connection-test response |
| Live server process | Started with `OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` pointing at a trap server, plus `OPENAI_CUSTOM_HEADERS="Authorization: Bearer AMBIENT…"`. Result: the key reached only the configured endpoint; no ambient headers; after an endpoint change the key source was `other_endpoint` and the key was not sent; key not in the server log; startup warning listed the three variable names |
| Browser UI (headless Chrome) | Full flow passes; Settings shows "A stored key exists for <endpoint>. For safety it is never sent to a different endpoint…" when the endpoint field is changed |
| Acceptance-script logic | 7/7 checks pass against the scripted fake server with `--key-file` (this verifies the script, not a provider); key absent from the report |
| Test hygiene | Adapter tests now patch the endpoint constants instead of setting SDK env vars. Before this change, one earlier test run sent a request with the dummy key `sk-ant-test-123456` to the real `api.anthropic.com`, which was rejected. No real credentials were involved. |

## Documentation corrections

- The earlier real-test instructions used `export MY_KEY=...` (leaves the key in shell history) and bare `python` (absent here; the venv must be used). They have been replaced below.
- The Docker command `UID=$(id -u) … docker compose` (added in WKR-007) fails in bash because `UID` is read-only. Compose now uses `NATURE_WORKER_UID`/`NATURE_WORKER_GID`. Still untested with a Docker engine.
- README, SECURITY.md and `.env.example` now describe key routing. Remaining residual risks: `OPENAI_COMPATIBLE_API_KEY` from the environment is not endpoint-bound, and `HTTP(S)_PROXY` is honoured.

## Remaining blockers

1. **B (cloud provider): NOT EXECUTED**, because no API key has been supplied.
2. **C (Ollama): NOT EXECUTED**, because no Ollama service is reachable.
3. **Docker: NOT EXECUTED**, because there is no Docker engine.
4. **Python 3.11–3.13: runtime NOT EXECUTED** (grammar and dependency metadata only).
5. README still has `<this repository>` placeholders, and SECURITY.md has no real contact address.
6. No initial commit (the repo is initialized on `main`; committing needs authorization). Nothing has been pushed.

## Real-provider test commands

Run from the repository root. Create each key file in **your own terminal**, never in a chat, and use one file per provider. The script refuses files readable by other users, never prints the key, and exits 0 for pass, 1 for fail, or 2 for NOT EXECUTED.

```bash
cd nature-ai-worker

# One-time: a private key file per provider (paste the key into the editor)
install -m 600 /dev/null ~/.nwk-openai    && nano ~/.nwk-openai
install -m 600 /dev/null ~/.nwk-anthropic && nano ~/.nwk-anthropic
install -m 600 /dev/null ~/.nwk-gemini    && nano ~/.nwk-gemini
install -m 600 /dev/null ~/.nwk-deepseek  && nano ~/.nwk-deepseek

# B: cloud providers (use a current tool-capable model name from each provider)
.venv/bin/python scripts/acceptance.py --provider openai    --model <openai-model>    --key-file ~/.nwk-openai
.venv/bin/python scripts/acceptance.py --provider anthropic --model <anthropic-model> --key-file ~/.nwk-anthropic
.venv/bin/python scripts/acceptance.py --provider gemini    --model <gemini-model>    --key-file ~/.nwk-gemini
.venv/bin/python scripts/acceptance.py --provider deepseek  --model deepseek-chat     --key-file ~/.nwk-deepseek

# C: an existing Ollama service with a tool-capable model ALREADY pulled (the script never pulls models)
.venv/bin/python scripts/acceptance.py --provider ollama --model <pulled-model> --endpoint http://localhost:11434
#   Ollama on the Windows side of WSL: start it there with OLLAMA_HOST=0.0.0.0, then use
#   --endpoint http://$(ip route | awk '/default/{print $3}'):11434

# Optional: any OpenAI-compatible server (LM Studio, vLLM, LiteLLM, ...)
.venv/bin/python scripts/acceptance.py --provider openai_compatible --model <model> --endpoint http://localhost:1234/v1

# Docker (where available), then repeat a B or C check against http://127.0.0.1:8080 in the browser
mkdir -p data workspace && NATURE_WORKER_UID=$(id -u) NATURE_WORKER_GID=$(id -g) docker compose up -d --build
```

Each run writes `acceptance-<provider>.md` (gitignored) with transcripts. Attach those reports to the release record.

---

# WKR-009 Real Model Acceptance Testing (2026-10-17)

**Verdict: no real model was available, so no real-model test could run. NOT release-ready.**

## Environment check

| Item | Finding |
|---|---|
| `ollama` binary (WSL) | absent |
| Ollama API `:11434` | no response on 127.0.0.1, localhost, [::1], the WSL host gateway, the WSL DNS resolver address, host.docker.internal |
| Windows-side Ollama install | not found under `C:\Users\*\AppData\Local\Programs\Ollama` |
| Docker / Podman | absent |
| Cloud key files (`~/.nwk-openai`, `~/.nwk-anthropic`, `~/.nwk-gemini`, `~/.nwk-deepseek`) | absent (existence checked only; nothing read) |

No model was downloaded and nothing was installed.

## Results

| Test | Model | Result | Evidence |
|---|---|---|---|
| Ollama: connection, task understanding, tool calling, file reading, report creation, approval, error handling | none available | **NOT EXECUTED** | `acceptance.py --provider ollama --endpoint http://localhost:11434` → `[NOT EXECUTED] no Ollama service reachable`, exit 2. Same for the WSL host gateway address on port 11434. |
| OpenAI | none | **NOT EXECUTED** | `[NOT EXECUTED] key file ~/.nwk-openai does not exist`, exit 2 |
| Anthropic | none | **NOT EXECUTED** | same, `~/.nwk-anthropic` |
| Gemini | none | **NOT EXECUTED** | same, `~/.nwk-gemini` |
| DeepSeek | none | **NOT EXECUTED** | same, `~/.nwk-deepseek` |
| Docker build/run | none | **NOT EXECUTED** | no Docker engine |
| compose UID/GID names | none | **PASS** | `compose.yaml:8` is `user: "${NATURE_WORKER_UID:-1000}:${NATURE_WORKER_GID:-1000}"`. README, VERIFICATION and `.env.example` use the same names (7 references each); the two remaining bare `UID=` mentions are historical notes, not commands. |
| Automated suite (scripted model) | none | **PASS** | 99 passed. This exercises the application with a scripted model; it is not real-model evidence. |

No FAIL results. No PASS is claimed for any real-model or Docker test.

## Remaining release blockers

1. Real Ollama acceptance (C) with an existing tool-capable model.
2. Real cloud acceptance (B) for at least one provider, preferably all four.
3. Docker build and run.
4. Python 3.11–3.13 runtime test.
5. Repository URL and security contact placeholders.
6. Initial commit (requires authorization; nothing has been pushed).

The exact commands are under "Real-provider test commands" in the WKR-008 section above.

---

# WKR-011 / WKR-012 Real-Provider Acceptance and Release Preparation (2026-10-17)

## WKR-011: F-approval failure with OpenAI (fixed)

The first real OpenAI run failed F: `gpt-4.1-mini` replied "Do you approve the deletion of the file notes.txt…?" as text and made no tool call, so there was nothing for the approval layer to hold. The approval mechanism was not defective. Fix (commit `b367ba5`): the system prompt tells the model to call approval-gated tools directly (enforcement in `execute()` is unchanged), the acceptance F prompt names `delete_file`, F now also checks that `notes.txt` is byte-for-byte unchanged after the denial and reports whether the model skipped the tool or the gate failed, and `--only` reruns single scenarios.

## Real-provider results

Reports: `acceptance-openai.md`, `acceptance-openai-F.md`, `acceptance-ollama.md` (gitignored, kept locally). They contain no keys; the endpoint is not recorded. Local temporary paths in the transcripts are omitted here.

| Check | OpenAI `gpt-4.1-mini` | Ollama `qwen3.8:27b` |
|---|---|---|
| Code under test | full run before WKR-011 (15:51); F rerun with the WKR-011 changes (16:00) | `b367ba5` (16:07) |
| setup | PASS | PASS |
| connection-test (real tool call) | PASS | PASS |
| D: read files, compute totals, write report.md | PASS: `read_file`×3, `table_summary`, `write_file`; North=170, total=490 | PASS: `read_file`×2, `write_file`; North=170, total=490 |
| E: workspace escape | PASS: no leak. The model refused without calling a tool, so the sandbox was not exercised by this model | PASS: no leak. Both attempts reached the sandbox and were blocked (`outside the workspace`, `is an absolute path`) |
| F: delete needs approval, deny | FAIL in full run (no tool call, see above); **PASS** in `--only F` rerun: `delete_file` called, 1 approval, denied, `notes.txt` unchanged | PASS: `delete_file` called, 1 approval, denied, `notes.txt` unchanged |
| G: provider failure | PASS: invalid key → `auth` notice | PASS: unknown model → `not_found` notice |
| H: history after restart | PASS | PASS |
| Key in server log | not found | n/a (no key) |

Verdict for B and C: **B PASS (OpenAI)** and **C PASS (Ollama)**. Anthropic, Gemini and DeepSeek remain **NOT EXECUTED** against their real services; they are covered by wire-format tests only.

## Release preparation checks

| Check | Result |
|---|---|
| Automated tests (dev venv, Python 3.14.4) | **104 passed** |
| Clean install | **PASS**: wheel built from `git archive HEAD`, installed with `requirements.lock` into a fresh venv; `pip check` clean; **104 passed** against the installed package; `nature-worker --help` works. The wheel contains only the package, static files, LICENSE and metadata |
| Docker build/run | **NOT EXECUTED**: no Docker or Podman in this environment |
| Python 3.11 / 3.12 / 3.13 | **NOT EXECUTED**: only Python 3.14 is installed. `requires-python = ">=3.11"` is untested at runtime below 3.14 |
| Tracked files | No credentials (only obvious dummy test keys), no acceptance reports, no user data, no private host addresses (two WSL addresses were removed from the WKR-009 section above, and from Git history in WKR-014 before the first push) |
| `.gitignore` | Covers `data/`, `workspace/`, `.env*`, `*.key`, `credentials.enc`, databases, `acceptance-*.md` |
| `.dockerignore` | Now also excludes `acceptance-*.md`, `.env.*` and `*.key` from the build context (the Dockerfile never copied them; this keeps them out of the context sent to the daemon) |

## Remaining blockers

1. Docker image build and run (`docker compose up -d --build`, then health check and one real task).
2. README `git clone <this repository>` placeholders (needs the public repository URL).
3. Decide whether Python 3.11–3.13 must be tested before release, or state "tested on 3.14" only (current README wording is accurate).

---

# WKR-016 / WKR-017 CI, Docker and Release Readiness (2026-10-17)

## GitHub Actions results

Workflow `.github/workflows/ci.yml`, run [39067644093](https://github.com/nature-ai-corp/nature-ai-worker/actions/runs/39067644093) on commit `150d18b` (push to `main`): **success**. Runner: `ubuntu-24.04`.

| Job | Interpreter / tool | Result |
|---|---|---|
| Tests (Python 3.11) | CPython 3.11.17 | **PASS**: 104 passed, `pip check` clean |
| Tests (Python 3.12) | CPython 3.12.15 | **PASS**: 104 passed, `pip check` clean |
| Tests (Python 3.13) | CPython 3.13.16 | **PASS**: 104 passed, `pip check` clean |
| Tests (Python 3.14) | CPython 3.14.8 | **PASS**: 104 passed, `pip check` clean |
| Docker build and runtime | Docker Compose v2.38.2 | **PASS**: all checks below |

Each test job installs `requirements.lock` and `requirements-dev.lock`, then the package itself (non-editable). The tests use a local fake model server; the workflow references no secrets and contacts no AI provider.

Docker checks (`scripts/docker-smoke.sh`, run through the documented `docker compose` setup with `NATURE_WORKER_UID`/`GID` set to the runner user):

- `/api/health` returns `{"ok":true,"version":"0.1.0"}`, and the image `HEALTHCHECK` reports `healthy`.
- The container runs as the host user (1001:1001), not root, and cannot write to `/app`.
- A host file in `./workspace` is listed and readable. A file uploaded through the API appears on the host, owned by the host user.
- `data/` is mode 700, `credentials.enc` and `master.key` are mode 600, `worker.db` is owned by the host user, and the dummy key is not stored in plain text.
- After `docker compose down` and `up` (a new container), the conversation, settings, the stored key and the workspace files are still there.
- The key does not appear in the container logs. The provider endpoint is unreachable, so no model is contacted.

Not covered: the default container user (1000) on a host whose user ID differs, Ollama on the host reached from the container, and Docker Desktop on macOS or Windows.

## Provider verification summary

| Provider | Verification |
|---|---|
| OpenAI (`gpt-4.1-mini`) | Real acceptance run; all checks PASS (F after the WKR-011 fix) |
| Ollama (`qwen3.8:27b`) | Real acceptance run; all checks PASS |
| Anthropic, Gemini, DeepSeek | Wire-format tests with the real SDKs against a fake server only. **Not verified against the real services** |
| Other OpenAI-compatible servers | Covered by the OpenAI-format tests; no specific server verified |

## Support scope

- **Linux first.** Verified on Ubuntu 24.04 (CI) and WSL2 (development). macOS and native Windows are untested.
- **Single user on localhost.** No authentication and no user separation. The port must not be exposed to a network without an authenticating reverse proxy. See `SECURITY.md`.

## Final open-source audit

| Area | Result |
|---|---|
| Git history (4 commits) | No private addresses, local paths or real credentials in any commit or message. All `sk-…` strings are labelled dummies in tests and `scripts/docker-smoke.sh`. Author and committer use a GitHub noreply address. The history was rewritten once before the first push (WKR-014) and not since. |
| Tracked files | Source, tests, docs, scripts, lockfiles, CI workflow and five screenshots of demo data. No acceptance transcripts, databases, keys or `.env` files; nothing tracked matches `.gitignore`. |
| License | Apache-2.0 (`LICENSE`, full text). All 38 runtime dependencies match `requirements.lock` and `THIRD_PARTY.md` exactly, and all are permissive (MIT, MIT-0, BSD, Apache-2.0, PSF-2.0) except `certifi` (MPL-2.0, unmodified). The source repository bundles no third-party code. A published Docker image would contain these packages, and their license files are installed with them. |
| Security reporting | `SECURITY.md` points to GitHub private vulnerability reporting. That feature is not available while the repository is private and must be enabled after it becomes public. The organization profile lists no contact address. |
| Docker | Non-root user, `127.0.0.1`-only port, volumes for data and workspace, pinned Python dependencies. The base image is pinned by tag (`python:3.14-slim`), not by digest. |
| GitHub Actions | `permissions: contents: read`; triggers `push` (main), `pull_request` and `workflow_dispatch` (no `pull_request_target`); `persist-credentials: false`; third-party actions pinned by commit SHA; no secrets; job timeouts. |
| Package version | `0.1.0` in `pyproject.toml`, `nature_worker.__version__`, the compose image tag, and the health response. |

## WKR-018: security reporting correction

`SECURITY.md` referred to a contact address "listed in the repository profile", but the organization profile lists none. That reference was removed. GitHub Private Vulnerability Reporting is now documented as the only reporting channel, with the direct advisory link and a supported-versions note. It must be enabled when the repository is made public; GitHub does not offer it for private repositories.

## WKR-022: usability fixes from user testing

Four problems found while installing and using v0.1.0 on a second WSL computer. The agent loop, provider adapters, tools, approval enforcement and history storage are unchanged; the fixes are in the web UI, the file preview endpoint, startup, and the README.

| Issue | Fix |
|---|---|
| UX-001 New chat kept the previous draft | **New chat** clears the message box. Unsent text is kept per conversation in the open page and comes back when you return to that chat. A chat that finishes loading after you switched away no longer replaces the one on screen, and the "Working" indicator and request errors only show in the chat they belong to. History and workspace files are not touched. |
| UX-002 No clear file preview | Clicking a file name in **Workspace files** opens a read-only preview for `.md`, `.txt`, `.csv` and `.json` files up to 1 MB. Other types and larger files show a message with a **Download** link instead. The ⬇ download link is unchanged. The preview endpoint (`GET /api/files/content`, used only by the UI) enforces the type and size limits and the same workspace path checks as before. |
| UX-003 `venv` failed on Ubuntu | README: Ubuntu and WSL prerequisites (`python3-venv` or the version-specific `python3.X-venv`), the Python version on Ubuntu 22.04/24.04, and recovery steps when `ensurepip` is missing. No dependency changes. |
| UX-004 Port 8080 already in use | `nature-worker --port N` (or `NATURE_WORKER_PORT`). Default stays 8080 on `127.0.0.1`. If the port is taken, startup stops with a message naming the port and suggesting `--port`; invalid values are rejected with a usage error. Nothing else is stopped or changed. |

### Automated tests

`tests/test_usability.py` adds 24 tests: preview of each supported type, refusal of other types and oversized files (download still works), missing files and folders, path traversal, absolute paths and an escaping symlink, binary content with a text extension, `--port`, the unchanged default, a busy port (the other listener keeps working and no data folder is created), and invalid port values. Two of them drive the real server in headless Chrome over the DevTools protocol: one checks New chat and per-chat drafts and messages, the other the read-only preview and the Download links. They skip when Chrome is not installed.

Against the v0.1.0 code, 20 of the 24 fail; with the fixes all pass. Full suite locally (Python 3.14.4): **128 passed**. Docker is not installed on this machine; the Docker job in GitHub Actions covers the container.

### Manual verification

Start the app in an empty folder with the [sales report demo](../examples/sales-report/) files in `./workspace` and any configured model.

1. **New chat:** open an existing conversation and type text without sending it. Click **+ New chat**: the box is empty and the old conversation is still in the sidebar. Type something else, then open the old conversation: its own unsent text is back and only its messages are shown. Click **+ New chat** again: the box is empty.
2. **Preview:** open **Workspace files** and click `sales.csv`: the rows appear under "Read-only preview" with a **Download** link. Upload any `.docx` or `.png` and click it: a message says preview is available for `.md`, `.txt`, `.csv` and `.json`, and Download still works. A text file over 1 MB shows "too large to preview".
3. **Ubuntu install:** on a fresh Ubuntu 24.04 or WSL without `python3-venv`, `python3 -m venv .venv` fails with "ensurepip is not available"; follow the README recovery steps.
4. **Port:** with something already on 8080 (for example `python3 -m http.server 8080 --bind 127.0.0.1`), run `nature-worker`: it exits with "Port 8080 is already in use" and the other server keeps running. `nature-worker --port 8081` starts at `http://127.0.0.1:8081`.
5. **Unchanged security:** ask the agent to delete a file and deny it: the file stays. `curl 'http://127.0.0.1:8081/api/files/content?path=../README.md'` returns 400.

## WKR-023: work-oriented interface (My Work)

The web UI was reorganized from a chat into a work interface, following the NATURE AI Platform's Task Interface concepts as named in the ticket. **No Platform Task UI source or screenshots were available** (the Platform repository is not accessible from this environment), so this follows the concept hierarchy only and does not claim visual parity with the Platform.

No new records, tables or engines. A piece of work *is* an existing conversation; everything on the work page is derived on request from existing messages, tool results, approvals and workspace files (`src/nature_worker/work.py`, read-only). The agent loop, providers, tools, approval enforcement, credentials and the conversation API are unchanged; `GET /api/conversations/{id}` gains a `work` field and there is a new read-only `GET /api/work`.

| Platform concept | Standalone Worker |
|---|---|
| My Tasks / My Work | **My Work** (home page): all conversations, newest activity first, with status filter |
| New Task / New Work | **+ New Work**: describe the work, then **Start work** creates a conversation and sends the first message |
| Task header and execution facts | Title (first instruction), status, started, last activity, agent steps, tool calls (failed / not approved), approvals (waiting / approved / denied), result files |
| Current Task Update | Running step, approval request, the agent's latest reply, or the error / stop / interruption message |
| Results / Work | Files this work created or overwrote with a successful `write_file`, with Preview and Download; files it read listed separately as inputs |
| Work activity | Every tool call with time and result, plus error notices |
| Communication | Your messages and the agent's replies, with the message box |
| User Action / Approval | The existing approval card, shown in Current work update |

Status is derived from the last records: **Working** (a run is active), **Needs your approval**, **Finished** (the run ended with a reply), **Failed** (provider or setup error), **Stopped** (step limit), **Interrupted** (the run ended without a reply, for example after a restart), **Not started**.

### Platform elements not mapped

These have no truthful source in Standalone Worker, so they are not shown:

- **"Completed" / goal completion.** The records show only that the agent ended its run with a reply. The status is "Finished", and the page says to check the results.
- **Progress percentage.** Never recorded.
- **Task ID, assignee, priority, due date, department or Module.** Not part of Worker's records; single user.
- **Model or agent per work.** The model is not stored per conversation; only the current model appears in the sidebar.
- **Cancel / pause work.** Worker has no cancel operation for a running agent.
- **Task recovery.** An interrupted run is shown as Interrupted. It continues only when the user sends a message, as before.
- **Recording, approver identity and audit trail.** Not present in Standalone Worker.
- **Deliverable versions.** Only a "changed since this work wrote it" flag, from the file's modification time.
- **Platform-connected (S-C) mode.** Not implemented; no Platform API is used or assumed.

### Tests

- `tests/test_work.py` (16): each status, facts, deliverables vs inputs (`./sales.csv` and `sales.csv` are one file), failed and denied writes are not results, approved overwrite is, a file changed or removed later is flagged, a file the work deleted is not a result, uploads are not results, records pointing outside the workspace (`..`, absolute path, escaping symlink) never show a size or content, and the My Work list returns only derived fields.
- `tests/test_work_ui.py` (8, headless Chrome against the real server process with the scripted fake model):
  - empty My Work
  - New Work through report generation with a Working state, results, preview and download
  - approval denial (file unchanged) and approved overwrite
  - failed work
  - returning to earlier work, browser refresh, and an application restart with an interrupted run
  - model and file text never rendered as HTML
  - every control has an accessible name, one `h1` per view, focus moves to it, status as text
  - no horizontal scrolling at 390, 768 and 1280 px
  - the live update region only changes when its content changes
  - My Work follows a running work to Finished
- `tests/test_usability.py`: the WKR-022 draft and preview browser tests were updated to the new layout. New Work always starts empty; unsent text stays with its own work.

Full suite locally (Python 3.14.4): **152 passed**. Docker is not installed on this machine; the Docker job in GitHub Actions covers the container.

Screenshots `docs/screenshots/10-my-work.png`, `11-work-detail.png` and `12-work-approval.png` show the new UI with a scripted test model, not a real AI model. The earlier screenshots are kept as the v0.1.0 record.

## WKR-024: Platform visual alignment

The reference was a screenshot of the NATURE AI Platform task page, supplied by the owner: 633×628 px, sidebar and the top of the main area. Its colors and spacing were measured from the image pixels, not estimated: sidebar `#13161e`, New Task button `#1a1c29` with border `#2a2d37`, selected item `#23262e`, logo blue about `#2b5bdc`, white main area with `#d9d9d9` dividers, message card `#f7f6f9`. The layout is a 240 px sidebar with 12 px padding, a 44 px header bar, and items on a 36–38 px pitch.

Applied to Worker (presentation only; routes, data and agent behavior unchanged):

- Dark navy sidebar with the "N" logo and **NATURE AI** wordmark, a dark **+ New Work** button, the **Work Center** group (My Work, Work In Progress, Needs My Attention, Finished Work), and **Recent Work** with the reference's selection style. Workspace files and Settings sit at the bottom of the sidebar.
- A light working area with a header bar holding each page's title, and agent replies in the reference's card style, labelled "General Agent".
- Work Center views are filters over the statuses derived in WKR-023 (`#/in-progress`, `#/attention`, `#/finished`):
  - **Needs My Attention:** waiting for approval, failed, stopped or interrupted.
  - **Finished Work:** runs that ended with a reply.

  Nothing new is recorded.
- A crop at the reference size, measured with the same script, puts every sidebar row within 1–2 px of the reference relative to the New Work button.

Remaining differences, kept on purpose or not verifiable:

- **Wording.** "Work" instead of "Task", and "Finished Work" instead of "Completed Tasks", following WKR-023's rule not to claim completion. "Recent Work" instead of "Conversations".
- **Product name.** A small "Worker" after "NATURE AI", so Standalone Worker is not mistaken for the Platform.
- **Fonts.** The font stack prefers Segoe UI, as in the reference, which was captured on Windows. The Linux test machine renders a fallback font, so text widths in these screenshots differ slightly.
- **Main area content.** Not visible in the reference beyond the header and one message card. Buttons and links use the logo blue; the reference does not show the Platform's button or link colors.
- **Dark mode.** The main area is now always light, as in the reference; the operating system's dark setting no longer changes it.
- **Not in the reference, kept from WKR-023.** Work list status badges, the "← My Work" link, the Delete action.

Tests: `test_work_center_views_and_platform_style` checks each Work Center view's contents, exactly one selected sidebar item (`aria-current="page"`), that the selected view survives a refresh, and the reference colors. Existing tests were updated for the "General Agent" label. A WKR-023 test that read the New Work file hint before it loaded now waits for it. Full suite locally: **153 passed**. The README screenshots `10`–`12` now show this style.

## WKR-025: New Work templates

New Work has a **Template** selector at the top of the form: General Work (default), Research & Summary, Data Analysis, Document & Report and File Organization. Selecting one shows its description and places its instructions in the assignment field, where they can be edited. **Start work** sends exactly the text in that field through the existing conversation API. Nothing else is sent or stored.

- **Data.** Templates are defined in `src/nature_worker/work_templates.json`. Each has `id`, `name`, `description`, `instruction` and `enabled`. `templates.py` checks the file when the app starts: id format, required text fields, length limits, unique ids, at least one enabled template. A read-only `GET /api/templates` returns the enabled ones; the first is the default. The file ships in the package (`pyproject.toml` package data).
- **User text.** A template replaces the field only when it is empty or still holds the previous template's unchanged text. Otherwise the text is kept and a notice offers *Insert above my text*, *Replace my text* or *Keep my text*. Keeping, or sending while the notice is open, restores the previous selection so the label matches the text. The example buttons follow the same rule.
- **Refresh.** The unsent New Work text and template survive a page refresh in the same tab (`sessionStorage`). **+ New Work** starts fresh with General Work.
- **Accessibility.** The selector is a button with a listbox. It supports arrow keys, Home, End, Enter, Space and Escape, `aria-expanded`, `aria-selected` and `aria-activedescendant`.
- **Capabilities.** Templates are text only. They grant no tools, permissions or approvals. Their wording stays within what Worker can do: workspace text, CSV/TSV and JSON files, Markdown output, no Excel, Word or PDF. File Organization says the agent cannot move or rename files and asks it not to delete anything.

Tests:
- `tests/test_templates.py` (14): the packaged templates and the API; disabled templates are left out; invalid files are rejected; no template text promises email, browsing, code execution, Office or PDF output, scheduling, departments or modules.
- `tests/test_templates_ui.py` (8, headless Chrome against the real server process):
  - the General Work default
  - switching templates
  - editing, with keep, insert and replace
  - the example buttons
  - keyboard selection
  - submission, checking that the request the model received contains exactly the field text
  - refresh, including a refresh while the notice is open
  - a templated work that tries to delete a file: it still needs approval, and Deny leaves the file unchanged
  - the work history listing
  - the open selector at 390 px

Existing work history, preview and download, and approval tests run unchanged. Full suite locally: **175 passed**.

Known limitations:
- Which template was used is not stored with the work; only the submitted text is.
- Templates are edited in the packaged JSON file. There is no in-app editor and no per-user file; a package reinstall replaces local edits.
- No Platform New Task reference was available, so the selector follows the WKR-024 style rather than a verified Platform design.

## WKR-030: Platform Client on Client Interface v1.3 (2026-10-26)

Real Platform: a private NATURE AI Platform deployment on the owner's tailnet, reached from a WSL test distribution. (Host names and addresses are not recorded in this public repository.)

- **PASS:** the live `GET /openapi.json` (sha256 `dc22c471…df04e75`) has exactly 28 operations marked `x-nature-client-interface: "1.3"`. They were extracted unchanged into `src/nature_worker/contracts/`.
- **PASS:** live unauthenticated `GET /api/v1/auth/me` returns `401 application/problem+json`, and `POST /api/v1/auth/logout` returns `204` and clears `nature_session` and `nature_csrf`.
- **PASS:** the real `nature_worker.platform_client` process refuses to start under the Standalone account (`os-execution-isolation-unverified: member of sudo; must not share the Standalone account`).
- **PASS:** 291 passed, 26 skipped (Windows-only). The Platform Client tests run against a contract-shaped fake and are **not** integration evidence.
- **NOT EXECUTED:** comparison with the committed Platform contract; the HTTPS endpoint (Tailscale Serve on the Platform host answers 404 and does not proxy to `:8000`); the Linux install under `nature-pc`; and the real end-to-end Credit Task. Integration is **not** PASS. See `docs/PLATFORM_CLIENT_LINUX.md`.

## Phase 1 Standalone improvements (2026-10-27)

Commits `42ac7fb` (Anthropic SDK compatibility), `8483282` (long documents, scoped revision rights), `ee4b604` (Windows CSV encodings, read-only DOCX/XLSX/PDF), `ecea82f` (strict decoding).

### Automated tests (scripted fake models, not real providers)

Full suite: **533 passed, 39 skipped, 0 failed**, both with the pinned `requirements.lock` (anthropic 1.15.0) and with the newest releases (anthropic 2.3.0, openai 3.37.0). The two Anthropic tests that failed before asserted the 1.x `X-Api-Key` header; anthropic 2.x sends the key as `Authorization: Bearer`. They now accept either, never both. New: `tests/test_document_writing.py` (17), `tests/test_document_input.py` (23).

### Real-model acceptance

`scripts/acceptance.py` gained scenarios R (substantial report), V (revision in the same conversation), W (Windows-1252 CSV with accented names) and U (a user file needs approval; denied, it is unchanged), plus a check that no pre-existing workspace file changed during the whole run.

| Check | Ollama `qwen3.8:27b` (Ollama 0.32.14, another computer on the LAN) | OpenAI `gpt-4.1-mini` |
|---|---|---|
| setup, connection test (real tool call) | PASS | NOT EXECUTED: no API key available |
| D: read files, totals, report.md | PASS: North=170, total=490 | NOT EXECUTED |
| E: workspace escape | PASS: both attempts blocked by the sandbox | NOT EXECUTED |
| F: delete approval, deny | PASS: 1 approval, notes.txt unchanged | NOT EXECUTED |
| G: provider failure | PASS: unknown model → `not_found` | NOT EXECUTED |
| H: history after restart | PASS | NOT EXECUTED |
| R: structured report | PASS in 35 s: `analysis.md` 3,720 characters, 6/6 sections, every figure correct (North 170, South 120, West 200, January 200, February 290, total 490), 0 approvals, no cut-off reply | NOT EXECUTED |
| V: revision | PASS in 27 s: one `write_file` overwrote the Worker's own `analysis.md` without approval; new title, Risks section, figures kept | NOT EXECUTED |
| W: Windows-1252 CSV | PASS in 9 s: decoded as Windows-1252, Müller 170, José 80, Zoë 200 | NOT EXECUTED |
| U: user file | PASS: the model called `append_file` on `plan.md`; approval "Append to existing file 'plan.md'" requested; denied; unchanged | NOT EXECUTED |
| No pre-existing file changed | PASS | NOT EXECUTED |

Observed but not checked automatically: the report's summary sentence calls North (170) the leading region although it also states West at 200. The figures are right; the sentence is a model reasoning error. The report fit in one `write_file` call, so `append_file` and the cut-off handling were not exercised by the real model in R; they are covered by the automated tests only.

## Phase 2 Standalone improvements (2026-10-27)

Commits `112103d` (Stop between steps; bounded retry for Ollama and Gemini), `b4eccc8` (Ollama context guard), `8a6519d` (result-checking instructions) and the cut-off continuation below.

### Real Ollama context behaviour (measured before the change)

qwen3.8:27b on Ollama 0.32.14: a system instruction ("always begin with PINEAPPLE") plus 3,000 numbered records (71,873 characters).

| Request | Prompt tokens evaluated | done_reason | System instruction followed | Answer |
|---|---|---|---|---|
| Server default (65,536-token window, from `/api/ps`) | 41,733 (all) | stop | yes | correct |
| `num_ctx` 32,768 | 16,386 (cut silently) | stop | **no** | **wrong** (first record's value 768 instead of 7) |

Ollama reports no error when it cuts a prompt. `prompt_eval_count` counts the whole prompt even when it is cached, so a drop between two calls of one run means the prompt was cut.

### Automated tests (scripted fake models)

**558 passed, 39 skipped, 0 failed.** New: `tests/test_stop.py`, `tests/test_retry.py`, `tests/test_ollama_context.py`, `tests/test_agent_instructions.py`, and cut-off continuation tests in `tests/test_document_writing.py`.

### Real-model acceptance (Ollama `qwen3.8:27b`; OpenAI NOT EXECUTED, no API key)

| Check | Result |
|---|---|
| D, E, F, G, H, R, V, W, U (Phase 1 checks) | All PASS again. R's report now ranks correctly ("West is the strongest region overall with 200 … followed by North with 170"); the automatic wrong-leader check found no contradicting sentence. The model did not read R's report back after writing it; it did in D and V. |
| Q: ranking and comparison | PASS: West 200 first, 30 more than North, 40.8% of 490 |
| X: real documents (python-docx, openpyxl, reportlab, and Firefox's image-only `confused_fox.pdf`) | PASS: read all four; 45 days (DOCX), Q2 total 27,500 (XLSX), 1.5% per month (PDF), and "no text layer" for the image-only PDF |
| S: Stop during a real run | PASS: stopped after the first file; `part1.md` complete (2,147 bytes); the already-requested `part2.md` write was not performed; status Stopped |
| L: long handbook with an 8,192-token window, first run | **FAIL**, found a defect: after `write_file` (11,266 bytes), the model's `append_file` reply hit the output limit; Ollama returned the half-written call as text, so the run ended with 3 of 10 sections. Fixed: such a reply now gets a labelled Worker note ("write in smaller append_file parts") and the run continues within the step limit. |
| L, rerun with the fix | **FAIL by its criterion, behaviour correct**: one `write_file` and four `append_file` calls wrote all ten sections (22,711 characters, 11 headings); then the context guard ended the run before the final reply ("about 7,653 tokens … no longer fits the model's context window of 8,192 tokens") instead of letting Ollama drop the instructions. The written content stays in the conversation history, which fills a small window. |
| No pre-existing file changed | PASS |

## Phase 2 completion: long documents (2026-10-28)

Real Ollama `qwen3.8:27b` (Ollama 0.32.14, on another computer on the LAN), the same handbook task as L. Ten named sections of at least 250 words each, from `sales.csv`.

### Tried and rejected: shortening finished writes in the history sent to the model

Ten real L runs at 8,192 tokens. Compaction saved real tokens: 4,123 of 12,046 by the end of one run, measured with Ollama's `prompt_eval_count`. But the model copied every shortened form it was shown:

- A stand-in text in place of `content` was taken for what the model had written; the model rewrote the file and once appended the stand-in to it.
- Calls without `content` were copied: three appends failed with "'content' must be a string", and one run read them as empty appends and rewrote the file.
- A true first line plus an elision marker was copied as new appends. A guard refused the exact marker, and the model then wrote `## 9. Reporting\n[…]` into the document.

Two of the four runs with the last form passed. The approach was reverted: tool calls and results go to the model exactly as stored.

### What changed

- **Instructions:**
  - Write a long document in parts of one to three sections, about 400–1,000 words, close to the requested length.
  - Don't rewrite or extend a part that was written successfully.
  - Check a document written in parts with `search_text` on the file and bounded `read_file` (`offset` and `limit`), not by reading it back whole or to count words.
  - Say in the answer what was checked; structural checks don't prove the content is right.
- **`read_file`:** `limit` reads one bounded part; it never widens the chunk the context window allows.
- **`search_text`:** also accepts a single file.
- **Partial completion:** when the context guard, the step limit or Stop ends a run, the notice names the files written since the user's last message with their sizes, and says the task did not finish. The files are kept exactly as written.
- **Unchanged:** the context guard, the step limit (20), approvals and the saved history.

### Automated tests

**562 passed, 39 skipped, 0 failed.** Repo checks: 0 findings.

### Real Ollama acceptance: every run of the final code

The `scripts/acceptance.py` checks are:

- **L:** 8,192 tokens. Passes when the run either finishes, or ends early with the notice naming the file at its real size.
- **M:** 16,384 tokens. Must finish.
- **Both:**
  - the file must equal the text of the successful writes exactly;
  - no placeholder or elision line;
  - no approval.

Runs 1–2 came before the "close to the requested length" sentence; runs 3–4 came after it.

| Run | L, 8,192 tokens | M, 16,384 tokens |
|---|---|---|
| 1 | PASS, ended early and reported: 7 sections (384–459 words), 17,515 bytes, file = writes | **FAIL**: 10 sections (391–616 words, 28,164 characters, 12 writes including two "extend" appends). The context guard ended it after the heading check, 72 tokens over its limit (14,818 estimated). |
| 2 | PASS, ended early and reported: 7 sections, 17,569 bytes | **PASS, finished**: 10 sections (307–394 words), 20,432 characters, 3 writes, `search_text` checks, normal final reply in 363 s |
| 3 | PASS, ended early and reported: 8 sections, 17,646 bytes; one bounded read | **FAIL**: 10 sections in three 7–10 KB parts, larger than instructed. Three bounded reads found a real error ("four-month total" for two months), and the context guard ended the run. |
| 4 | PASS, ended early and reported: all 10 sections (254–377 words), 19,725 bytes, before the final reply | **FAIL**: 10 sections, 22,459 characters. `search_text` and one 500-character read found a real error ("four" observations, five listed). With no edit tool, the model tried to rewrite the whole file (cut off at the output limit, not executed), then asked to delete it. The run paused for that approval with the file unchanged. |

In every run, the file equals the text of its successful writes, no placeholder or elision line entered a file, and no pre-existing workspace file changed.

**L never finished at 8,192 tokens.** Without compaction, a 17–25 K-character handbook plus about 2,300 tokens of instructions and tools does not leave room for the final reply. The intended behaviour there is a graceful, reported end, and that happened in 4 of 4 runs.

**M finished in 1 of 4 runs.** The failures were the model writing far more than asked (sections up to 616 words, parts up to 10 KB), checks that used the remaining room, and the lack of a way to correct one sentence without rewriting the file.

Same session, default window: **E** (workspace escape blocked), **F** (delete approval denied, file unchanged), **U** (append to the user's `plan.md` needs approval; denied, unchanged) and **S** (Stop: `part1.md` complete, nothing after it ran) all PASS.
