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
2. Make your change with tests; run `pytest` and `python scripts/repo_checks.py`.
3. Sign off every commit (`git commit -s`, see below).
4. Open a PR describing what changed and why, and how it was tested.

## Intellectual property of contributions

**License of contributions.** Under Section 5 of the [Apache License 2.0](LICENSE), anything you intentionally submit for inclusion is licensed under the Apache License 2.0, without additional terms. You keep the copyright in your contribution. The project does not ask you to assign it.

**Developer Certificate of Origin.** Each commit must carry a `Signed-off-by: Your Name <email>` line (`git commit -s`). By adding it you certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/), in summary:

- you wrote the contribution, or have the right to submit it under the Apache License 2.0;
- if it is based on other work, that work is under a compatible open-source license, and you say which;
- you understand that the contribution and your sign-off are public and kept permanently.

If you contribute as part of your job, make sure your employer allows it.

**What you must not contribute:**

- Code, documents or assets you do not have the right to license under Apache-2.0, including code copied from projects under incompatible licenses (GPL, AGPL, LGPL, SSPL, "source-available" or non-commercial licenses).
- Any part of the proprietary NATURE AI Platform server: its source, configuration, database schemas, internal documentation, or business rules beyond the published Client Interface contract. The Worker talks to the Platform only through that interface.
- Credentials of any kind (API keys, tokens, passwords, private keys, certificates), customer data, internal host names, IP addresses or network details, and screenshots that show any of these.
- Binaries, build outputs, release bundles or packaged dependencies.

`scripts/repo_checks.py` (run in CI) checks for many of these. It catches accidents; it is not a substitute for care. If you committed a secret, treat it as exposed: revoke it first, then report it as described in [SECURITY.md](SECURITY.md). Removing it in a later commit does not remove it from git history.

**New dependencies** must have an Apache-2.0-compatible license. State the license in the PR, and add the package to `THIRD_PARTY.md`.

**Names and logo.** Contributions are welcome under the project's name, but forks and modified distributions must follow [TRADEMARKS.md](TRADEMARKS.md).
