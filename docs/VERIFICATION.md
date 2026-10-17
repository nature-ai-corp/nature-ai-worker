# v0.1 Verification Record

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
