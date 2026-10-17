# Security Policy

## Reporting a vulnerability

Please do not open a public issue, discussion or pull request for security problems.

Report them privately with GitHub **Private Vulnerability Reporting**:

1. Open the repository's **Security** tab and click **Report a vulnerability**, or go directly to <https://github.com/nature-ai-corp/nature-ai-worker/security/advisories/new>.
2. Describe the problem, the affected version (for example `0.1.0`, shown by `/api/health`), and steps to reproduce. Do not include real API keys or private data.

The report is visible only to you and the maintainers. We aim to acknowledge reports within 5 working days, and we coordinate a fix and disclosure through the advisory. You need a GitHub account to file a report.

## Supported versions

Security fixes are made for the latest release (currently 0.1.x).

## Security model

Nature AI Worker is a **single-user application meant to run on your own computer**. It has no user accounts.

| Area | Protection |
|---|---|
| Network exposure | Binds to `127.0.0.1` by default; Docker Compose publishes on `127.0.0.1` only. `Host` header allow-list (DNS-rebinding protection). State-changing requests with a foreign `Origin` are rejected (CSRF protection). |
| Workspace confinement | Every path is resolved with `realpath` (following symlinks) and must stay inside the workspace. Absolute paths, `..` traversal and symlinks pointing outside are rejected. Files are opened with `O_NOFOLLOW`. |
| Approvals | Overwriting or deleting a file and fetching a URL need explicit user approval. This is enforced in the tool execution layer (`tools.execute`), not by the prompt: a call that needs approval cannot run until the user approves that exact stored call. Approvals are single-use. |
| No code execution | There is no shell or Python execution tool. `calculate` uses a whitelist-based AST evaluator. |
| Web fetch | Disabled by default. When enabled: http/https only, no embedded credentials, and every connection (including redirects) is checked at connect time. Loopback, private, link-local (including cloud metadata `169.254.169.254`), CGNAT, multicast and reserved addresses are blocked. The socket connects to the validated IP, which prevents DNS rebinding. Environment proxies are ignored. Size, time and redirect limits apply. Content is marked as untrusted for the model. |
| Prompt injection | File and web content is treated as data, and the system prompt tells the model not to follow instructions in it. This is a mitigation, not a guarantee. The approval gate is the real control for destructive or outbound actions. |
| API keys | Encrypted at rest (Fernet) in a `0600` file, or supplied by environment variables. Never stored in SQLite, never returned by the API, never added to conversation history, and redacted from logs (including tracebacks) and from provider error messages and replies. |
| Key routing | Official providers always use their explicit official endpoints. SDK environment overrides (base URLs, custom headers, ambient tokens, Vertex switch) are hidden while SDK clients are constructed, so they cannot redirect keys or inject credentials. Keys saved for a configurable endpoint (Ollama, OpenAI-compatible) are bound to that exact endpoint and are not sent anywhere else. See README, "How API keys are stored". |

## Known residual risks

- **Anyone who can reach the port can use the agent.** Do not bind to `0.0.0.0` or publish the port on a network without an authenticating reverse proxy.
- **Local users with access to the data directory** can decrypt the stored keys if the generated `master.key` is used. Set `NATURE_WORKER_SECRET_KEY` or use environment variables to avoid this.
- **The model can read every file in the workspace** and send its contents to the configured AI provider. Only put files there that you are willing to share with that provider.
- **The model can create new files** in the workspace without approval. It cannot overwrite or delete files without approval.
- Path validation and file opening are not atomic for intermediate directories. Another local process racing to swap directories for symlinks inside the workspace could defeat the check. This needs local write access to the workspace.
- If you approve a web fetch, the URL itself can carry data (for example, in query parameters). Review URLs before approving.
- A **hard link** placed in the workspace by a local user that points to a file outside it cannot be detected by path checks. The agent could read that file. Overwriting it still needs approval.
- `OPENAI_COMPATIBLE_API_KEY`, if set as an environment variable, is sent to whichever OpenAI-compatible endpoint is configured in Settings. Keys saved in the UI are bound to their endpoint; environment keys are not.
- Provider requests honour the standard `HTTP(S)_PROXY` variables. Over HTTPS, a proxy cannot read the key unless you also trust its certificate authority.
- The upload endpoint reads the whole request into memory before enforcing its 20 MB limit. This only matters to someone who can already reach the local port.
