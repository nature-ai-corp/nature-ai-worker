# Windows Platform Client (S–C)

Status: **implemented; NOT yet executed on a real Windows installation.** Unit tests pass on Linux, with a recording fake of Windows. The native-API tests and the installed-service end-to-end test (`scripts/platform_client_windows_e2e.py`) still have to run on Windows. See [Verification](#verification-status).

## What it is

The same thin client as on Linux ([PLATFORM_CLIENT_LINUX.md](PLATFORM_CLIENT_LINUX.md)), with the same code, UI, preflight and Client Interface 1.3 contract. It runs as a Windows service under its own Windows identity. There is no Windows-specific Platform API, and no local Agent, Task or approval logic: every Task, Agent run and approval is the Platform's.

| | Standalone | Platform Client (Windows) | Platform Client (Linux) |
|---|---|---|---|
| Process | `nature_worker`, started by the user | Windows service `NatureWorkerPlatformClient` | systemd unit `nature-worker-platform-client` |
| Identity | the user | virtual account `NT SERVICE\NatureWorkerPlatformClient` | system account `nature-pc` |
| Code | `%LOCALAPPDATA%\NatureAIWorker` (user) | `%ProgramFiles%\NatureAIWorker\PlatformClient` (administrators) | `/opt/nature-worker-platform-client` (root) |
| Data | `%LOCALAPPDATA%\NatureAIWorker\data` | `%ProgramData%\NatureAIWorker\PlatformClient` | `/var/lib/nature-worker-platform-client` |
| Configuration | environment | service registry `Environment` (administrators only) | `/etc/nature-worker-platform-client.env` (root) |
| UI | `127.0.0.1:8080` | `127.0.0.1:8090` (loopback only) | `127.0.0.1:8090` |

## Isolation

The Linux rules apply, using Windows mechanisms:

- **Identity.**
  - The service runs as its own virtual account, which is never the Standalone user.
  - The account isn't an administrator.
  - The service keeps only `SeChangeNotifyPrivilege` (`sc.exe privs`), the one privilege every process needs to traverse folders.
- **Data root.**
  - The data root has an explicit, protected DACL with entries for SYSTEM and Administrators (full control) and the service account (modify), and nothing else.
  - No Users, Authenticated Users, Everyone or Standalone-user entry is allowed.
  - Its parent `%ProgramData%\NatureAIWorker` is restricted to SYSTEM and Administrators.
  - The installer refuses folders that a non-administrator created (anti-squatting), as well as links and junctions.
- **Code.**
  - Code lives under Program Files and can be changed by administrators only.
  - The service runs `runtime\python.exe`, a copy of the installing Python made into the installation. A per-user Python can't serve here for two reasons: the service account couldn't read it, and the Standalone user could change it and so run code as the service.
- **Preflight.** Before the service listens, `windows_isolation.py` checks the following and refuses to start if any check fails:
  - the process token isn't an administrator, isn't elevated and isn't SYSTEM;
  - the token isn't the recorded Standalone user's SID;
  - the data root DACL is private to the token;
  - the two data roots don't overlap.
- **Credentials.** As on Linux, Platform session cookies are held in memory only. Nothing is written to Credential Manager or DPAPI, and the password is never stored.

- **Elevated tools.** The installer and upgrader run `sc.exe`, `icacls.exe` and PowerShell by their absolute System32 path, never by bare name. Windows would otherwise look in the calling interpreter's folder first, which can be user-writable.

Not covered, and stated rather than assumed:

- **Runtime provenance.** The runtime is copied from the Python that runs the installer, and is trusted at that moment. If that Python lives in a user-writable folder, as a uv-managed or per-user install does, malware already running as that user before installation could have altered it. Prefer an all-users python.org installation under Program Files as the source.
- **Administrators.** Administrators, including an administrator user who approves a UAC prompt, can read any file or process. The Standalone Agent runs with the user's non-elevated token, in which Administrators is deny-only.

## Install

The installer needs administrator rights and asks for them with a UAC prompt. Use a python.org Python or a relocatable CPython build, 3.11 or later; it becomes the service's runtime. The Microsoft Store Python is refused.

```bat
py -3.12 scripts\platform-client-windows.py install --origin https://<platform-host> ^
    --standalone-data %LOCALAPPDATA%\NatureAIWorker\data
```

This installs exactly the committed `HEAD`. On a computer without git, pass `--archive SRC.tar`, a tar made with `git archive --format=tar HEAD`; it carries its commit ID. The steps:

1. Check administrator rights, the origin (`https://` only), the data root (refused if it is a Standalone root or was created by someone else) and the port.
2. Stop a running instance after letting running submissions finish. The service's shutdown signs out of the Platform.
3. Copy the runtime and build `versions\<version>+g<commit>` from the wheels in `requirements.lock`.
4. Set the data root's permissions and mark it `platform_client`.
5. Register the build. This uses the same layout and `current` pointer as `nature-worker-upgrade`.
6. Create or update the service, then start it. The service reports *running* only once it answers. The installer then waits until `/api/status` names this build and origin.
7. Register in Apps & Features (Uninstall, and Modify for repair) and add a Start-menu link to `http://127.0.0.1:8090/`.

If a reinstall fails at any step, the build that was serving before is reactivated. Builds made this way are unsigned local builds, never reported as official.

Once installed, the commands are in `%ProgramFiles%\NatureAIWorker\PlatformClient\bin`:

| Command | Purpose |
|---|---|
| `nature-worker-platform-client-setup.cmd status` | installation, service state, data-root privacy, `/api/status` |
| `nature-worker-platform-client-setup.cmd repair` | re-apply the service configuration, privileges, permissions, launchers and registration for the active build. It never changes the version, the origin or the data. |
| `nature-worker-platform-client-setup.cmd uninstall [--delete-local-data]` | see below |
| `nature-worker-upgrade.cmd upgrade --bundle DIR` | signed upgrade (as administrator) |
| `nature-worker-upgrade.cmd status \| recover \| rollback \| history` | as for every managed installation ([UPGRADES.md](UPGRADES.md)) |

## Upgrade

Upgrades use the existing transactional upgrade engine through `WindowsPlatformClientToolkit`. The engine runs, in order: verify the signed bundle; let running submissions finish; stop the service; back up the data root; install the new version beside the old; migrate; start the service on the new version and check `/api/status`. If anything fails, it rolls back automatically. The upgrade journal makes an interrupted upgrade recoverable.

Data-root work runs as the administrator. Files it creates inherit the data root's DACL, and files it moves are reset to it (`icacls <root>\* /reset`). The data root's own DACL is never reset.

## Uninstall and installation modes

- **Uninstall.**
  - Lets running submissions finish, stops and deletes the service, and removes the code, the Apps & Features entry and the Start-menu link.
  - Prints the manifest of the local data, and deletes it only with `--delete-local-data`. Links inside are removed as links, never followed.
  - Platform server data and the Standalone installation are never touched.
  - Started from Apps & Features, it continues from a temporary copy in a new window, because files in use cannot be deleted.
- **Clean validation.** After uninstall, an independent check reports each probe:
  - credentials (none are persisted);
  - running Platform Client processes, by command line through CIM;
  - the service and Apps & Features registration;
  - the installation folders.
  
  Enterprise device policy can't be verified locally. Following [PLATFORM_CLIENT_SECURITY.md](PLATFORM_CLIENT_SECURITY.md), that keeps a fresh Standalone installation **not authorized** by this check, which is a product decision.
- **Modes.**
  - The Windows Platform Client is a separate, fresh installation, as on Linux. The Standalone installation is neither converted nor read.
  - `nature-worker-upgrade install` already refuses a Platform Client data root for Standalone.
  - There is no in-place switch from S–C back to Standalone.

## Verification status

| Check | Status | Evidence |
|---|---|---|
| Unit tests: SID derivation (checked against TrustedInstaller's known SID), DACL analysis, isolation preflight, server wiring, installer (install, refusals, anti-squatting, failed-reinstall reactivation, repair, uninstall, link safety, clean validation), layout, toolkit, service host | PASS (Linux) | `tests/test_windows_platform_client.py` |
| Native token, DACL and SCM reads; service host outside the SCM | NOT EXECUTED | Windows-only tests in the same file |
| Installed service end to end: install, process identity, private data, repair, signed upgrade, failed upgrade with rollback, tampered bundle, killed during verification plus recover, user rollback, uninstall, reinstall, clean validation | NOT EXECUTED | `python scripts\platform_client_windows_e2e.py --workdir NEW --archive SRC.tar` (elevated). It works only in its own namespace (service `NatureWorkerPlatformClientE2E`, `%ProgramFiles%\NatureAIWorkerE2E`, `%ProgramData%\NatureAIWorkerE2E`, its own Apps & Features key and Start-menu link), refuses to start if any of them exists, and finally checks that the production service, folders, their permissions, the Standalone folder and Credential Manager are unchanged. `NEW\e2e-journal.log` records every scenario and child process (PID, command, timeout, exit), Python stacks of a step stalled over 5 minutes or of a crash, and console/sign-out/shutdown events; a journal without the final `runner finished` line means the runner was stopped from outside or crashed. A run that ended without its own cleanup (e2e-4952: Windows restarted mid-run) is removed with `--cleanup-leftovers --workdir THAT_DIR` (elevated): the same e2e uninstall, refused while any installer, upgrader or e2e process is active or an upgrade is unfinished |
| Sign-in, Task, Agent results through the Windows service against the real Platform | NOT EXECUTED | manual, after the end-to-end test passes |
