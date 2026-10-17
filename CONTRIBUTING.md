# Contributing to Nature AI Worker

Thank you for your interest in contributing!

## Project scope

Nature AI Worker is deliberately small: **one general agent, a bounded tool loop, SQLite, and a plain web UI.** Before starting larger work, please open an issue to discuss it. The following are out of scope for this project:

- Multi-agent systems, agent/skill/workflow builders, or workflow engines
- Long-running autonomous work loops or task schedulers
- Context-management or task-recovery frameworks
- Organization, role, or enterprise administration features
- New infrastructure dependencies (Redis, PostgreSQL, Celery, React build chains, ...)

Good contributions: bug fixes, provider compatibility fixes, clearer error messages, accessibility and UI polish, security hardening, documentation, and tests.

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
nature-worker            # http://127.0.0.1:8080
```

## Guidelines

- Keep code simple and readable. Prefer the standard library and existing dependencies; justify any new dependency in the PR description, including its license (Apache-2.0 compatible only).
- Every behaviour change needs a test. Tests should exercise the real application where practical (see `tests/conftest.py` for the fake model server).
- Never log, persist, or return API keys. Never weaken workspace confinement or the approval gate without discussion.
- If you change provider adapters, state in the PR whether you verified against the real provider (`scripts/acceptance.py`) or only against the fake server.
- Do not contribute code or assets you do not have the right to license under Apache-2.0.

## Pull requests

1. Fork and create a branch.
2. Make your change with tests; run `pytest`.
3. Open a PR describing what changed and why, and how it was tested.

By contributing, you agree that your contributions are licensed under the Apache License 2.0.
