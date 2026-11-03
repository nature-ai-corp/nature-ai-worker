# Windows Standalone installer

A per-user Windows installer for Nature AI Worker (Standalone), built with Inno Setup. It needs
no Python, Git, WSL, Docker or administrator rights on the target computer.

> **Status (2026-11-03):** the official Ed25519 release public key (`64ecfc2da2ed12e9`) is
> registered in the release-candidate source, but no signed official release has been built,
> verified and published. The Windows installer still has no Authenticode certificate. The
> existing `v0.1.0` tag points to an earlier commit than this RC branch; do not publish this
> branch's build as `v0.1.0` without resolving the source/tag mismatch. See
> [Release blockers](#release-blockers), [RELEASES.md](RELEASES.md) and
> [VERIFICATION.md](VERIFICATION.md).

## What it installs

| | Location (defaults) |
|---|---|
| Program | `%LOCALAPPDATA%\Programs\NatureAIWorker` (`/DIR=`) |
| Data: settings, encrypted API keys, history, backups | `%LOCALAPPDATA%\NatureAIWorker\data` (`/DATADIR=`) |
| Workspace: the files the agent may use | `Documents\Nature AI Worker` (`/WORKSPACE=`) |
| Start menu | `Nature AI Worker` folder: *Nature AI Worker*, *Remove Nature AI Worker data*, *Licenses* |
| Apps & Features | *Nature AI Worker*, per user (uninstall) |

Apps & Features always shows the **active** version. Every version switch goes through the
upgrader's single `set_current`: install, upgrade, automatic rollback, user rollback and recover.
When the installer registered this installation (its entry's `InstallLocation` is this program
folder), `set_current` updates that entry's version.

The program folder is a normal managed installation ([UPGRADES.md](UPGRADES.md)). Each version
in `versions\<version>\` has its own `runtime\`, the pinned CPython, unpacked from the signed
release bundle, and a `venv\` built from that runtime and the bundle's wheels. A rollback
therefore returns to exactly the runtime that version was verified with.

The *Nature AI Worker* entry runs `bin\nature-worker-desktop.cmd`. If this installation's Worker
is already running, it only opens the page. Otherwise it starts the Worker in a console window,
on port 8080 or on a free port when 8080 is taken, and opens the browser. Closing the window
stops the Worker. Provider selection and configuration happen on first use, in the app's
Settings, exactly as before.

## How it works

The installer has no install logic of its own. Before anything is written, it:

1. extracts its payload and checks its SHA-256 (corruption check);
2. checks the bundled runtime archive against the pinned SHA-256, then unpacks it with Windows'
   own `tar.exe` into its temporary folder;
3. runs `bootstrap.py` with that runtime. The script installs the release's own upgrader
   offline: only the wheels named in the manifest, each hash-checked. It then calls the upgrader:
   - `nature-worker-upgrade install` for a new installation. The upgrader verifies the Ed25519
     manifest signature and every file, builds the version from the bundled runtime, checks that
     it starts, and only then records the installation.
   - `nature-worker-upgrade upgrade` for an existing installation, with the same engine and
     semantics as a command-line upgrade: running work finishes, the data is backed up, the
     version is switched, migrated and checked, and it rolls back on any failure.

Only when that succeeds does Setup add the Start-menu entries, the licenses and the
Apps & Features entry. If it fails, Setup stops on its "Preparing to install" page with the last
lines of the log. A failed new installation leaves no installation record. A failed upgrade
leaves the previous version active, or restored.

**Refusals (nothing is changed):**

- the same version is already installed;
- a newer version is installed (going back is `nature-worker-upgrade rollback`);
- a silent upgrade without `/CONFIRMUPGRADE=<new version>`;
- a non-empty target folder that is not an installation;
- an installation left without an active version by an interrupted upgrade (run `recover`);
- a command-line installation at `%LOCALAPPDATA%\NatureAIWorker`;
- data or workspace folders inside the program folder;
- **anything that would be an in-place use of the Platform Client (S-C):** a data folder marked as
  Platform Client data, or program, data or workspace inside the Platform Client's folders
  (`%ProgramFiles%\NatureAIWorker`, `%ProgramData%\NatureAIWorker`).

**Standalone and the Platform Client are separate installations.** They may exist on the same
computer, each with its own identity, folders and port:

| | Standalone | Platform Client |
|---|---|---|
| Identity | the user | its own service account |
| Folders | the user's own, by default | `%ProgramFiles%\...`, `%ProgramData%\...` (administrators only) |
| Port | 8080 | 8090 |

This is how the Platform Client is designed ([PLATFORM_CLIENT_WINDOWS.md](PLATFORM_CLIENT_WINDOWS.md)).
It records the Standalone user and data folder only to keep them apart. Its preflight refuses
the Standalone user's identity and overlapping data roots, and "the Standalone installation is
neither converted nor read".

What is never allowed is an **in-place switch**: turning a Platform Client installation, or its
data, into Standalone. Going from S-C back to Standalone means the Platform Client's uninstall,
explicit cleanup and clean validation, then a fresh Standalone installation
([PLATFORM_CLIENT_SECURITY.md](PLATFORM_CLIENT_SECURITY.md)).

The Standalone installer and upgrader enforce their side without looking at Platform Client
data. Platform Client folders are recognised by their path alone, before anything is opened, so
Standalone never opens, reads, lists or changes them. The mode marker of the chosen data folder
is checked as before.

**Uninstall** (Apps & Features) first runs `nature-worker-upgrade uninstall-check`. It refuses
while the Worker runs or an upgrade is unfinished. It then removes only what the installation
created (`versions`, `bin`, `upgrade`, the records, the licenses, the shortcuts and the
Apps & Features entry). **Data and workspace are kept.** Installing again adopts them, after a
backup.

**Removing data** is a separate, explicit step: Start menu > *Remove Nature AI Worker data*
(`nature-worker-upgrade remove-data`). It shows what will be deleted and requires the data folder
to be typed back exactly. It deletes the data folder (settings, saved keys, history, backups),
and the workspace only with `--include-workspace`. It refuses while the Worker runs and for
Platform Client data.

**Silent use:** `Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /LOG=setup.log [/DIR=] [/DATADIR=] [/WORKSPACE=] [/CONFIRMUPGRADE=]`.

## Trust

- **First installation:** the trust root is the installer file itself. Development builds are
  unsigned, so compare its SHA-256 with the published value. The manifest signature is still
  checked, by the upgrader shipped in the same installer.
- **Upgrades:** the installed upgrader's rules apply. Upgrades through a newer installer are
  verified by the release's own engine, against the keys the installation trusts.
- **Development builds:** these are signed with a development key. The installer passes its
  public part as a locally trusted key (`trusted-keys.json`), so every check reports "NOT an
  official NATURE AI release". No private key is ever in an installer. The build refuses to
  stage a file that looks like one.

## Building

Pinned inputs, checked byte for byte:

| Input | Pin |
|---|---|
| CPython 3.12.15, python-build-standalone 20261009, Windows x64 `install_only_stripped` | `packaging/windows/runtime.json` |
| The 42 dependency wheels of `requirements.lock` for Windows x64 / CPython 3.12 | `packaging/windows/wheels-win_amd64-cp312.sha256` (checked against PyPI's published digests) |
| Inno Setup 6.7.3 (build tool, not shipped) | `packaging/windows/innosetup.json` |
| License texts of the runtime's libraries | `packaging/windows/runtime-licenses/` (provenance in its README) |
| setuptools 84.0.0, the build backend of the Worker wheel | `packaging/build-requirements.txt` (hash; used without build isolation, so pip never fetches a build tool itself) |

Where each pin is checked:

- **Runtime:**
  - when staging (against `runtime.json`);
  - in the signed manifest;
  - by Setup before unpacking (`GetSHA256OfFile`);
  - by the upgrader's bundle verification;
  - by the kit before it uses the runtime.
- **Wheels:**
  - when staging (against the hash list);
  - in the signed manifest;
  - by `pip --require-hashes` in `bootstrap.py` and in every version's venv.
- **Inno Setup:** by the kit before installing it.
- **Payload:** by Setup (`PayloadSha256`, compiled in).

Nothing is fetched at install time.

1. Stage (any OS) from the committed `HEAD`:

   ```
   python scripts/windows_installer.py stage --private-key KEY.pem --out DIR [--trust-key KEYS.json]
   ```

   This builds the signed bundle (`release_bundle.py build --target windows-x86_64-3.12
   --wheel-hashes ... --runtime ...`) and the payload, the licenses and `defines.iss`.
2. Compile on Windows: `ISCC.exe DIR\NatureAIWorker.iss` → `DIR\output\NatureAIWorker-Setup-<version>.exe`.
   Publish its SHA-256 with it.

`windows_installer.py kit` builds the native acceptance kit: three installers (the product
version, a newer test version with one data migration, a test version that fails to start), the
driver `scripts/windows_standalone_acceptance.py` and numbered steps.

## Private release-candidate staging on the controlled Windows signing computer

Use `scripts/windows_rc_stage.ps1` to stage the current Windows-first RC from committed
source using the already-registered **official private signing key**. This is an
**internal test build**, not a GitHub Release: the current source reports version
`0.1.0`, but the pre-existing `v0.1.0` tag points to another commit. Signing
the manifest does **not** fix that version/tag mismatch.

Prerequisites: Windows, Git, Python 3.13 with the repository's pinned dependencies,
and a clean checkout of `release/v0.1.0-rc-windows-first`. Build or install
Inno Setup 6 to produce an EXE; without ISCC, the helper only stages the
installer files. Run from PowerShell on the **signing computer**, not in CI:

```powershell
git switch release/v0.1.0-rc-windows-first
git pull --ff-only
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps .
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows_rc_stage.ps1
```

The helper verifies the private key's public ID is `64ecfc2da2ed12e9`,
prompts without echoing the passphrase or putting it in shell history, stages
the signed Windows payload, checks the commit in `build-info.json` and,
when ISCC.exe is available, compiles the installer and prints its SHA-256.
You may provide `-ISCC 'C:\path\to\ISCC.exe'` if auto-discovery fails.

For compatibility with the existing Python build script, the helper temporarily
passes the passphrase to its subprocesses through
`NATURE_RELEASE_KEY_PASSPHRASE`, then removes that environment variable in
`finally`. On a shared or compromised signing host, environment variables
are not a substitute for OS account isolation. The private PEM stays outside
the source checkout and is never copied into the staged payload.

Record the build commit, bundle signature verification, installer SHA-256
and **actual install/uninstall results on the separate Testing Computer**.
Never upload the private key, passphrase, or this development EXE as a
claimed official release. Before real publication, resolve the version/tag
provenance, native acceptance, Authenticode and licensing blockers.

## Licenses

The installer shows the Apache-2.0 license. It installs `licenses\`, which holds `LICENSE.txt`,
`NOTICE.txt`, `THIRD_PARTY.md` and `THIRD_PARTY_LICENSES.txt`. That last file is generated from
the shipped files themselves:

- CPython's `LICENSE.txt`;
- the runtime's library licenses (OpenSSL 3, SQLite, libffi, zlib, liblzma, bzip2, expat,
  mpdecimal, libuuid, Tcl/Tk, Tix);
- pip;
- every wheel's license files.

The build fails if a wheel has neither a license file nor a declared license.

## Release blockers

- **Official release provenance:** the Ed25519 public key is registered, but no official signed
  bundle has passed all checks in [RELEASES.md](RELEASES.md). The existing `v0.1.0` tag is on an
  earlier commit than the RC branch. A release needs a distinct, matching version/tag and verified
  manifest/source provenance before publication; do not move or overwrite an existing tag.
- **Authenticode code signing:** none, so SmartScreen warns and the installer's own integrity rests
  on the published SHA-256.
- **Legal review:** see [Licensing review](#licensing-review) (Microsoft runtime terms, Inno
  Setup commercial-use request).
- **Native evidence on the current revision:** see VERIFICATION.md. Every result there names
  the tree it was built from.

## Licensing review

Reviewed 2026-10-28 from the shipped files themselves. This is not legal advice; the open items
need the owner's decision.

| Component | License | Status |
|---|---|---|
| Nature AI Worker | Apache-2.0 | LICENSE and NOTICE installed; the license is shown in Setup |
| Python packages (42 wheels) | permissive (MIT, BSD, Apache-2.0, PSF), certifi MPL-2.0 | every wheel's license files in `THIRD_PARTY_LICENSES.txt`; certifi unmodified, its source (`.py`) is in the wheel |
| CPython 3.12.15 | PSF-2.0 | `LICENSE.txt` reproduced |
| Runtime libraries: OpenSSL 3 (Apache-2.0), SQLite (public domain), libffi, expat (MIT), zlib, bzip2, liblzma, mpdecimal, libuuid, Tcl/Tk, Tix | permissive | texts reproduced from python-build-standalone's own license files; the stripped runtime omits them, so `runtime-licenses/` carries them |
| pip inside the runtime, and the pip wheel used for venvs | MIT, with vendored packages | license files reproduced |
| Microsoft Visual C++ runtime (`vcruntime140*.dll` in the runtime) | Microsoft Distributable Code terms (in CPython's Windows `LICENSE.txt`) | **open**, see 1 |
| Inno Setup code inside `Setup.exe` | Inno Setup License | notice kept in NOTICE; **open**, see 2 |

**Open legal issues:**

1. **Microsoft Distributable Code.** CPython's Windows `LICENSE.txt` says that whoever further
   distributes programs including Microsoft's Distributable Code "must require distributors and
   external end users to agree to terms that protect the Microsoft Distributable Code at least
   as much as Microsoft's own requirements". Setup shows only the Apache-2.0 license; the
   Microsoft terms are installed in `licenses\` but not presented for agreement. The owner
   should decide whether to present end-user terms that cover this, or confirm, with counsel,
   that the current notices suffice.
2. **Inno Setup commercial use.** The Inno Setup License permits commercial use. jrsoftware's
   ordering page also "request[s] that all commercial users of Inno Setup purchase licenses".
   The owner should decide whether NATURE AI buys one before distributing installers.
3. **OpenSSL NOTICE.** Apache-2.0 §4(d) requires passing on a NOTICE file if the work has one.
   The OpenSSL license text is reproduced. No OpenSSL NOTICE file was found in the shipped
   wheels or runtime licenses, but this was not checked against OpenSSL's 3.x source
   distribution.
