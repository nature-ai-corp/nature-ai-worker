# Linux Platform Client (S–C) on Client Interface v1.3

Status: **implemented; real Platform integration verified historically (Tasks #77, #78, reported by the maintainer), not re-run on the current revision.** See [Verification](#verification-status).

## What it is

`python -m nature_worker.platform_client` (console script `nature-worker-platform-client`) is a thin client of the NATURE AI Platform. It has no local Agent, model provider, tool, or Standalone data access. Templates, Task creation, Agent execution, authorization, approvals and files are all the Platform's. It is a **separate, fresh installation**: the existing Standalone installation and its data are not converted, read or modified.

| | Standalone | Platform Client |
|---|---|---|
| Process | `nature_worker` (`nature-worker`) | `nature_worker.platform_client` |
| OS account | the user's own | `nature-pc` (system account, no shell, no privileged groups) |
| Data root | e.g. `~/nature-ai-worker/data` | `/var/lib/nature-worker-platform-client` (0700, `nature-pc`) |
| UI | `127.0.0.1:8080` | `127.0.0.1:8090` (loopback only) |
| Mode marker | `standalone` | `platform_client` |

## Contract

- The contract is extracted unchanged from the running Platform's `/openapi.json` by `scripts/client_interface_contract.py`. It contains only the operations marked `x-nature-client-interface: "1.3"`, plus every schema and security scheme they reference. Provenance and hashes are in `contracts/client-interface-v1.provenance.json`.
- It has not yet been compared with the Platform repository's committed `docs/client-interface-v1.openapi.json`, because that file was not accessible from this host. Compare it with `python -I scripts/client_interface_contract.py compare <committed.json> src/nature_worker/contracts/client-interface-v1.openapi.json`.
- An operation the contract does not mark as Client Interface cannot be called. An incompatible session cookie, CSRF header or interface version fails closed at startup.

Operations used: native login and logout, `/auth/me`, conversations (create, list, read, messages, post message, the Task a conversation continues), Task templates, My Tasks, Task by number, work history, results (list, read), files (list, download), approvals (list, approve, reject), close Task, and continue a Task in chat. Not used: enterprise OIDC sign-in (a browser redirect flow) and recording authorization. No upload exists; see [decision 0001](decisions/0001-sc-file-upload.md).

## Authentication

- **Sign-in:** email and password are posted once to `nativeLogin` and then dropped. The password is never stored.
- **Cookies:** the Platform sets the `nature_session` and `nature_csrf` cookies. The Worker keeps them only in memory, inside an object that refuses to be printed or pickled. They are never placed in an HTTP cookie jar, sent to the browser, logged or persisted.
- **CSRF:** every POST, PUT, PATCH and DELETE repeats the `nature_csrf` value in `X-CSRF-Token`.
- **Validity:** a session lasts at most eight hours locally, or less if the cookies' `Max-Age` is shorter. The Platform remains the authority: any `401` ends the session in the Worker immediately.
- **Sign-out:** sign-out calls `logout`, and then checks that the old cookies get `401` from `/auth/me`. If they don't, the UI says the revocation is unconfirmed and keeps retrying. It never claims success.
- **Transport:**
  - The Worker accepts HTTPS only.
  - Plain HTTP is accepted only with `NWPC_ALLOW_INSECURE_LOOPBACK=1` and a loopback host, for local development such as an SSH tunnel.
  - A remote `http://` origin, including a Tailscale `100.x` address, is always refused.
  - Redirects are never followed.
- **Local UI:**
  - The browser gets its own random `nwpc_ui` cookie (HttpOnly, SameSite=Strict), and only after a successful Platform sign-in.
  - Without that cookie, no other local process can drive the session through port 8090.
  - Host and Origin checks apply as well. The Standalone Agent's web tool already blocks loopback and CGNAT addresses.

## Long-running work

- **Submissions:**
  - A submission is posted in the background with an `Idempotency-Key`. A retry replays the result and never duplicates the Task.
  - The read timeout is 20 minutes.
  - If the Platform does not answer, the state is "unknown - check My Tasks", not "failed".
- **Progress** comes only from Platform facts: `work_pending`, the newest work period, `latest_work_failed`/`latest_work_interrupted`, pending approvals, and the Agent's own Task Update.
- **Polling:** the UI polls every 3 s for the first minute, every 10 s up to 15 minutes, and every 30 s after that. Polling never stops on its own, and elapsed time is shown but never treated as a failure.

## Install (requires sudo; the user runs this)

```bash
sudo scripts/platform-client-linux-install.sh \
     --origin https://<platform-host>.<tailnet>.ts.net \
     --standalone-data /home/<user>/nature-ai-worker/data
```

The script:
1. Creates the `nature-pc` account.
2. Installs the code root-owned under `/opt/nature-worker-platform-client`.
3. Creates the empty private data root and marks it `platform_client`.
4. Records the Standalone owner's uid.
5. Runs the preflight as `nature-pc`.
6. Starts the hardened systemd unit only if the preflight passes. The unit sets `NoNewPrivileges`, an empty capability set, `ProtectSystem=strict`, `ProtectHome=read-only`, no core dumps and `UMask=0077`.

To remove it: `sudo scripts/platform-client-linux-uninstall.sh [--delete-local-data]`. Platform server data is never touched.

## Preflight

`check_platform_startup` must pass, or the process does not start:
- The data root is a `platform_client` installation.
- The contract has loaded and an origin is configured.
- Linux isolation is verified: the process runs under an unprivileged account with no sudo, wheel, admin, docker or lxd membership, and that account is not the Standalone owner. The data root is private to it and doesn't overlap the Standalone root.
- Credentials are memory-only.

Run it with `nature-worker-platform-client preflight`.

## Residual risks (not mitigated by this client)

- Root, and anyone who can become root, can read any process or file. On WSL that includes `wsl -u root` from Windows. The Standalone user is in `sudo`, but sudo is password-protected, so a compromised Agent cannot escalate without that password.
- Linux has no persistent revocation journal, unlike Windows' DPAPI journal. If the process is killed, its session cookies are lost from memory but stay valid on the Platform until they expire (at most eight hours), unless an administrator revokes them.
- The local composition of a template submission is the Worker's own choice: "Label: value" lines in area order, then the additional instructions. The contract only requires non-empty `content` with the `template_id`. Platform-side review should confirm that this matches the Portal.

## Verification status

| Check | Status | Evidence |
|---|---|---|
| Live contract retrieved; 28 Client Interface 1.3 operations | PASS | `GET /openapi.json`, sha256 `dc22c471…df04e75` |
| Unauthenticated `/auth/me` → 401 problem+json; `logout` clears both cookies | PASS | live responses, 2026-10-26 01:22 UTC |
| Compared with the committed Platform contract | NOT EXECUTED | committed file not accessible |
| Private HTTPS endpoint (Tailscale Serve) | NOT EXECUTED | Serve on the Platform host answers 404 for every path; it does not proxy to `:8000` yet |
| Linux install under a separate account | NOT EXECUTED | requires sudo |
| Real Worker login, Task, Agent run, progress, results, logout, audit | PASS (historical, reported by the maintainer); not re-run on the current revision | Linux Worker connected to the real Platform over the existing HTTPS Client Interface: Task #78 was created and executed and returned Agent results; Task #77 was inspected in the Worker UI. The run's logs and revision are not in this repository. |
| Unit tests against a contract-shaped fake | PASS | not integration evidence |
