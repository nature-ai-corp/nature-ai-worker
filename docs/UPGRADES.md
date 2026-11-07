# Upgrades

`nature-worker-upgrade` upgrades a **managed installation** of Nature AI Worker (Standalone or Platform Client) to a verified release, with the user's explicit authorization, and rolls back if anything fails.

> **Status (2026-11-06).** Standalone upgrades are implemented and tested with real processes on a disposable installation on Linux and on Windows (12 scenarios each, see [Verification](#verification-status)). Platform Client upgrades are implemented but **not yet exercised on a real installation**. The official release key (`64ecfc2da2ed12e9`) is registered, but no official signed release has been built yet ([RELEASES.md](RELEASES.md)). Not production-ready until the open rows in the verification table are closed.

## Commands

```
nature-worker-upgrade status [--bundle DIR|URL] [--json]   installed version, data schema, last upgrade;
                                                             with --bundle: the available release, notes, eligibility
nature-worker-upgrade check --bundle DIR|URL                verify a release and report eligibility; changes nothing
nature-worker-upgrade upgrade --bundle DIR|URL              verify, show notes, ask, upgrade (rolls back on failure)
           [--confirm-version X.Y.Z] [--drain-timeout S]    non-interactive authorization must name the exact version
nature-worker-upgrade recover [--confirm]                   finish or undo an interrupted upgrade
nature-worker-upgrade rollback [--confirm]                  go back to the version before the last upgrade
nature-worker-upgrade history [--json]                      every upgrade outcome recorded for this installation
nature-worker-upgrade install --bundle DIR ...              create a managed Standalone installation
nature-worker-upgrade uninstall-check                       Standalone: may the program be removed now? (used by
                                                             the Windows uninstaller; data is never touched)
nature-worker-upgrade remove-data [--include-workspace]     Standalone: delete the data root (and the workspace)
           [--confirm-path DIR]                             after the exact folder is typed back
```

Exit codes: `0` done, `1` failed (rolled back, or repair needed), `2` not eligible / not authorized / nothing changed, `3` usage or installation error.

The Standalone web UI shows the installed version and, after a `check`, the available release and its eligibility in **Settings**. The upgrade itself runs from the command line, because it has to stop the server that serves the page. When a page that is still open notices that a different version now answers, it shows "Nature AI Worker was upgraded … Reload".

## Installations

| | Standalone | Platform Client (Linux) |
|---|---|---|
| Created by | `nature-worker-upgrade install` (as the user) | `sudo scripts/platform-client-linux-install.sh` |
| Code | `~/.local/share/nature-ai-worker/` (Linux), `%LOCALAPPDATA%\NatureAIWorker\` (Windows), user-owned | `/opt/nature-worker-platform-client/`, root-owned |
| Data | any data root you name; existing data is adopted in place, after a backup | `/var/lib/nature-worker-platform-client`, owned by `nature-pc`, mode 0700 |
| Runs as | the user | `nature-pc` (systemd) |
| Upgrade runs as | the same user; root is refused | root (`sudo …/bin/nature-worker-upgrade`); data work runs as `nature-pc` |
| After the upgrade | not restarted: start it again with `bin/nature-worker` | the service is restarted if it was running; users sign in again |

Each installation has this layout:

```
<prefix>/installation.json      edition, data root, account/service (fixed at install)
<prefix>/current                active version (one line, replaced atomically)
<prefix>/versions/<version>/    one complete venv per version, never modified once complete
<prefix>/bin/                   launchers that run versions/<current>
<prefix>/upgrade/               lock, journal (state.json), history.jsonl, logs/
<prefix>/trusted-keys.json      optional locally trusted keys (never official)
```

**Windows installer.** The per-user Windows installer ([WINDOWS_STANDALONE_INSTALLER.md](WINDOWS_STANDALONE_INSTALLER.md)) creates and upgrades exactly this layout through `install` and `upgrade`. Its release bundles also carry a pinned Python runtime (manifest field `runtime`, hashed and signed like every wheel). Each version unpacks that runtime into `versions/<version>/runtime/` and builds its venv from it, so the installation needs no Python of its own and a rollback returns to the version's own runtime. Upgraders that predate the field ignore it and use their own interpreter.

**Existing installations.** A source checkout started with `pip install .` and `nature-worker` (the README's quick start) is *not* managed: upgrade it with `git pull` and `pip install .` as before. Its data migrations now run automatically when the Worker starts, after a backup to `<data>/backups/`. To switch to managed upgrades, run `nature-worker-upgrade install --bundle … --data-dir <your data> --workspace <your workspace>`. Your data stays where it is.

The **Linux Platform Client** install script now builds into `versions/<version>+g<commit>/` and records the build as an *unsigned local build*. When it is run over an installation with the older single-venv layout (`/opt/…/venv`), it switches the service to the new launcher and removes the old venv only after the new service is active.

**Docker** deployments are upgraded by building or pulling a new image; data migrations run, after a backup, when the container starts. The upgrader does not manage containers.

## What an upgrade does

```
verify bundle -> eligibility checks -> show notes, ask for authorization
installing   new version built in versions/<new>; the active version is untouched
stopping     data root marked "upgrade in progress"; the running Worker finishes its work and exits
backing_up   <data>/backups/upgrade-<id> (database via SQLite online backup; keys stay 0600)
switching    current -> new version (one atomic file replace)
migrating    the NEW code migrates the data (each step in one SQLite transaction)
verifying    the new version starts on the real data and answers; installation mode unchanged
complete     marker removed, history records the outcome
```

Every phase is written to `upgrade/state.json` before it starts.

**Verification of a release** (before anything is installed): the bundle is copied (or downloaded over HTTPS) into a private staging directory. The manifest's Ed25519 signature must verify against a trusted key, then every file must match its size and SHA-256. pip installs only those wheels, offline: `--no-index --require-hashes --only-binary=:all:`, with pip configuration and `PIP_*`/`PYTHON*` variables removed. No source package is built.

**Eligibility:** the release is newer than the installed version; the installed version is at least `upgrade_from.min`; the release supports this edition; it targets this OS, CPU architecture and Python version; its data schema is not older than the data's; the installation mode is unchanged. For the Platform Client, the release must speak the same Client Interface version as the installed client. A Platform Client upgrade therefore never runs ahead of the Platform's shared interface. Finally: the OS permission checks pass, and no other upgrade is unfinished.

**Authorization:** interactive, by typing the target version, or with `--confirm-version X.Y.Z`. That flag must name exactly the version being installed. There is no `--yes`.

**Stopping safely:** the upgrader writes `maintenance-request.json` into the data root. The Worker then rejects new work (HTTP 503 "stopping for an upgrade"), waits for running Agent runs to finish, and exits. Approvals waiting for the user are stored and survive. A Platform Client finishes the submissions it is waiting on, signs out of the Platform (which revokes the session), and exits. If work is still running after `--drain-timeout` (600 s by default), the upgrade is **aborted**: the request is withdrawn, the Worker carries on, and nothing changes. A newly installed version directory may remain, ready for the next attempt.

**Preserved:** the workspace is never touched. The data root (settings, history, approvals, encrypted API keys and the master key) is kept, migrated in place, and backed up first. The installation record, the installation mode and the OS accounts are never changed.

## Failures, rollback and recovery

- **Failure before `switching`:** nothing has changed; the previous version stays active.
- **Failure from `switching` on (same run):** automatic rollback. `current` returns to the previous version. If migrations ran, the data root is restored from this upgrade's own backup, taken seconds earlier from the previous version with the Worker stopped. The data being replaced is moved to `<data>/backups/failed-upgrade-…`, never deleted. The previous version is then health-checked. Outcome: `rolled_back` (exit 1).
- **Rollback fails too:** outcome `repair_required`. The upgrade marker stays, so the Worker refuses to start until `nature-worker-upgrade recover` succeeds. Old code never runs on migrated data.
- **Interrupted** (killed, crash, power loss): the Worker refuses to start ("An upgrade … did not finish"). `recover` reads the journal and shows what it will do, then waits for confirmation:
  - interrupted before anything changed: it cleans up;
  - interrupted during backup, switch or migration: it restores the previous version and this upgrade's backup;
  - interrupted during verification (data fully migrated): it verifies the new version again and keeps it if it works, otherwise it rolls back.
- **Rollback by request** (`rollback`), after a successful upgrade:
  - if the previous version can read the current data schema, the data is kept as it is;
  - otherwise the backup from that upgrade is restored. The prompt says so, and work done since then is moved aside, not deleted.
  - Refused if the previous version or the needed backup no longer exists.
- **Data from a newer version:** every version refuses to open data with a newer schema than it knows, instead of guessing.

## History and privacy

`upgrade/history.jsonl` records each attempt: time, outcome, from/to versions, signer key id and scope, manifest SHA-256, failed phase, error, and backup location. It never records API keys, session cookies or the upgrade token (only its SHA-256 is stored in the data-root marker). Health-check logs are kept in `upgrade/logs/`. The Worker already redacts API keys from its own logs.

## Edition separation

- An upgrade never writes the installation-mode marker. It verifies after the upgrade that the marker is unchanged, and rolls back if it is not.
- `install` refuses a Platform Client data root for Standalone.
- The Standalone upgrader refuses to run as root, and requires the installation and data to be owned by the user.
- The Platform Client upgrader requires root (Linux) or an elevated administrator (Windows) and a code path that only those can write. It requires a data root private to the Platform Client account. On Linux, schema and migration work runs as that account (`runuser`). On Windows it runs as the administrator, and files keep the data root's DACL (SYSTEM, Administrators, the service account). It touches no Standalone path.
- A Platform Client release must match the installed Client Interface version. The upgrader cannot install Platform business logic: releases contain only the Worker and its dependencies.

## Verification status

| What | Status | Evidence |
|---|---|---|
| Unit tests: release verification, layout, launchers, migrations, drain handshake, engine (every failure point, interruption and recovery path, both rollbacks), CLI authorization, Platform Client toolkit with a recording runner | PASS (Linux) | `tests/test_release.py`, `test_upgrade_engine.py`, `test_upgrade_layout_cli.py`, `test_migrations_maintenance.py` |
| Real Standalone upgrades on Linux, disposable installation: install over v0.1 data; upgrade while Agent work runs; drain timeout; refusals (tampered, untrusted, altered, downgrade, wrong platform, incompatible path, unauthorized); failed upgrade with automatic rollback; kill during migration plus recover; kill during verification plus recover; user rollback; edition separation | PASS | `python scripts/upgrade_e2e.py --workdir <new dir>` (report written there; CI job `upgrade-e2e`) |
| Real Standalone upgrades on Windows | PASS | `scripts/upgrade_e2e.py`, 12/12 scenarios, on Windows 11 (Python 3.13.14) on 2026-11-06 for the trees of `57a9f15` and `9bd594a`; CI job `upgrade-e2e` on `windows-2022` passed on `4fc4b38`. An earlier run (2026-10-26, 8 of 12) failed on `state.json` sharing violations, fixed in `7eb07bb`. |
| Real Platform Client upgrade on Linux (root, systemd, `nature-pc`) | NOT EXECUTED | requires sudo; see [Manual Platform Client upgrade test](#manual-platform-client-upgrade-test) |
| Platform Client upgrades on Windows (service, `WindowsPlatformClientToolkit`) | IMPLEMENTED; unit-tested on Linux; NOT EXECUTED on Windows | `tests/test_windows_platform_client.py`; real run: `scripts/platform_client_windows_e2e.py` ([PLATFORM_CLIENT_WINDOWS.md](PLATFORM_CLIENT_WINDOWS.md)) |
| Updated `platform-client-linux-install.sh` (versioned layout, legacy conversion) | NOT EXECUTED | requires sudo; syntax and fragments checked only |
| Official release key | DONE | `64ecfc2da2ed12e9` in `src/nature_worker/upgrade/release_keys.json` (2026-11-03, [RELEASES.md](RELEASES.md)) |
| Signed official releases | NOT DONE | owner decision ([RELEASES.md](RELEASES.md)) |

## Manual Platform Client upgrade test

On a disposable Linux machine or VM, **not** on the machine with the Platform Client in use:

1. `sudo scripts/platform-client-linux-install.sh --origin https://… --standalone-data …`, then check the service is active and `/opt/nature-worker-platform-client/current` names the build.
2. Build two test bundles with a test key (see `scripts/upgrade_e2e.py` for how test versions are made). Add the key to `/opt/nature-worker-platform-client/trusted-keys.json` (root-owned, 0644).
3. Sign in, start a Task, then run `sudo /opt/nature-worker-platform-client/bin/nature-worker-upgrade upgrade --bundle <bundle>`. Check that:
   - the submission finished before the service stopped;
   - the session was revoked (the old cookies get 401);
   - the service runs the new version (`/api/status` `version`);
   - `history` records success.
4. Repeat with a broken bundle (automatic rollback), and with `kill -9` of the upgrader during `migrating` followed by `recover --confirm`.
