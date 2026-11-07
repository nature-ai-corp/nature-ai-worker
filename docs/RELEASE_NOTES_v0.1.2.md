# Nature AI Worker v0.1.2 — Release Notes

## Highlights

- **GitHub Community Agent (new, optional).** Work Center → *GitHub Community Agent* opens a prebuilt professional profile with four starter tasks: *Review Issues*, *Review Discussions*, *Draft Replies* and *Community Report*. Each starter fills an editable New Work task that you review and start yourself. It runs as ordinary Work on the same General Agent, with the same history, approvals and model settings. It is not a separate agent, scheduler or autonomous service.
- **Read GitHub Issues and Discussions (new, off by default).** In *Settings → Advanced*, choose one public repository as `owner/name` (default: `nature-ai-corp/nature-ai-worker`) and turn on GitHub reading. The agent can then read only that repository.
  - **Issues** and their comments are read from GitHub's public API without a token.
  - **Discussions** require a separate **read-only token**: a fine-grained token with *Public repositories (read-only)* access, or a classic token with no scopes. Tokens with broader classic scopes are refused.
- **Post one approved Issue comment (new, off by default, separate switch).** The agent can only *propose* a comment. You see the repository, the Issue, the GitHub account and the exact text, and nothing is posted unless you click *Approve and post*. Each approval posts at most once.
  - Requires its **own posting token**, never the read-only one: a fine-grained personal access token with an expiry date, limited to the selected repository with only *Issues: Read and write*. Classic tokens are refused.
  - Every attempt is recorded in a durable local outbox **before** it is sent.
  - If the outcome is uncertain (for example a timeout or a restart during sending), the comment is shown as **outcome unknown** and is **never sent again automatically**. *Reconcile* checks GitHub read-only.
  - Comments that contain credentials, private keys or local folder paths are refused.
- **What GitHub features do not do.** They cannot read private repositories or any repository other than the selected one. They cannot post to Discussions, edit or close Issues, change labels, or act on their own. GitHub text is treated as untrusted data, never as instructions or approval.

Details: [GitHub Community Agent](GITHUB_COMMUNITY_AGENT.md) and the *GitHub Community tools* section of the README.

## Upgrading

- **Windows installation (made with the v0.1.1 or an earlier 0.1.x Windows installer):** run the v0.1.2 installer while signed in as the same Windows user.
  - It upgrades the per-user installation in place and keeps settings, encrypted API keys, work history and workspace files.
  - Before changing anything, it verifies the release signature and every file.
  - It backs up the data folder before switching versions.
  - Running work is allowed to finish first, for up to 10 minutes. If work is still running after that, the upgrade stops and nothing is changed. Let the work finish, or stop it, then run the installer again.
  - A silent upgrade (`/VERYSILENT`) also needs `/CONFIRMUPGRADE=0.1.2`; without it, nothing is changed.
- **Use the installer, not `nature-worker-upgrade`, from installations made before v0.1.1.** Their upgrader may not include the official release key and then refuses the release. The installer always verifies with the key built into v0.1.2.
- **v0.1.0 run from source (`pip install`) or Docker Compose:** there is no in-place upgrade. Update the source and run `pip install .` again, or rebuild the Docker image. Keep the same data folder (default `./data`); v0.1.2 reads v0.1.0 data.
- The data format is unchanged from v0.1.1. The GitHub outbox tables are added automatically when v0.1.2 first opens the database. Older versions ignore them.
- All GitHub features stay **off** after the upgrade until you turn them on.
- **Going back:**
  - If v0.1.2 does not start and pass its checks during the upgrade, the previous version and its data are restored automatically.
  - After a successful upgrade, `nature-worker-upgrade rollback` (the `bin` folder of the installation, by default `%LOCALAPPDATA%\Programs\NatureAIWorker\bin`) returns to the previous version. Because the data format is unchanged, your data, including work done on v0.1.2, is kept.
  - `nature-worker-upgrade history` shows every upgrade outcome. See [UPGRADES.md](UPGRADES.md).
- Uninstalling keeps your data folder and workspace; deleting data is a separate, explicitly confirmed step.

## Requirements

- Windows 10 build 17134 or later, or a supported Windows 11 version, x64. Installation is per user and needs no administrator rights, system Python, Git, WSL or Docker.
- A configured AI provider or local model (provider usage may be charged by the provider). GitHub features additionally need network access to `api.github.com`.
- Standalone and Platform Client remain separate modes with separate data and credentials; the GitHub features exist only in Standalone.

## Download verification

Download the Windows installer only from the GitHub Release of the `v0.1.2` tag, and check its SHA-256 against `SHA256SUMS.txt` published with the release. The release bundle inside the installer carries an Ed25519 manifest signed by the official release key listed in `src/nature_worker/upgrade/release_keys.json`; its source commit must equal the commit of the `v0.1.2` tag. Validation evidence for this release is recorded in the release pull request on GitHub.

## Open-source license

Nature AI Worker is licensed under the Apache License 2.0. The installer includes the license and attribution files of the bundled Python runtime and third-party libraries; see `LICENSE`, `NOTICE`, `THIRD_PARTY.md` and the installed `licenses` folder.
