# Real-Model Testing

Results stay **NOT EXECUTED** until a run below finishes with real model calls. Record a result as PASS only from an `acceptance-<provider>.md` report produced by an exit-code-0 run. Exit codes: `0` all checks passed, `1` a check failed, `2` not executed (service, model or key unavailable).

Each run starts the real server in a temporary folder and checks: model connection with a tool call, a natural-language task that reads `sales.csv` and `notes.txt` and writes `report.md` (expects North = 170, total = 490), a workspace escape attempt, approval enforcement (delete → deny), a provider error, and history after restart.

## Setup (any machine)

```bash
# This repository has no remote. To copy it to another computer:
git bundle create nature-ai-worker.bundle main          # on this machine
git clone nature-ai-worker.bundle nature-ai-worker      # on the other machine

cd nature-ai-worker
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                                     # expect: 99 passed
```

## 1. One real cloud provider

Create the key file in **your own terminal** (never paste a key into a chat, a command line or a shared document):

```bash
install -m 600 /dev/null ~/.nwk-openai && nano ~/.nwk-openai     # paste the key, save
.venv/bin/python scripts/acceptance.py --provider openai --model <tool-capable model> --key-file ~/.nwk-openai
```

To rerun only some scenarios, add `--only` with comma-separated letters (for example `--only F`). Setup always runs; the connection test runs only in a full run. A focused run writes `acceptance-<provider>-<letters>.md`, so the full report is kept.

For another provider, replace `openai` (both in `--provider` and the file name) with `anthropic`, `gemini` or `deepseek`. The script refuses key files that other users can read and never prints the key. Delete the key file when you are done (`rm ~/.nwk-openai`).

## 2. Ollama on another computer

**Option A: run everything on the Ollama computer (simplest).** Copy the repo there with the bundle above, make sure a tool-capable model is already pulled (`ollama list`), then:

```bash
.venv/bin/python scripts/acceptance.py --provider ollama --model <model from ollama list> --endpoint http://localhost:11434
```

**Option B: Worker on this computer, Ollama on computer B over the LAN.**

On computer B, Ollama must listen on the network. By default it listens on localhost only:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve     # or set OLLAMA_HOST for the Ollama service/app and restart it
ollama list                                # confirm a tool-capable model is present
```

On this computer:

```bash
curl http://<B-ip>:11434/api/tags          # must list the model
.venv/bin/python scripts/acceptance.py --provider ollama --model <model> --endpoint http://<B-ip>:11434
```

Ollama has **no authentication**. Expose it only on a trusted network, restrict port 11434 in the firewall to this computer if possible, and set `OLLAMA_HOST` back to localhost afterwards.

## Recording results

Add a dated section to `docs/VERIFICATION.md` with: the provider, the exact model name, the script's PASS/FAIL lines, and the tool calls shown in the report (for example `read_file`, `write_file`, `delete_file` → approval → denied). Keep the generated `acceptance-*.md` reports as evidence; they are gitignored and contain no keys.
