# Windows Standalone installer

A per-user Windows installer for Nature AI Worker (Standalone), built with Inno Setup. It needs
no Python, Git, WSL, Docker or administrator rights on the target computer.

> **Status (2026-11-03):** the official Ed25519 release public key (`64ecfc2da2ed12e9`) is
> registered in the release-candidate source, but no official release has completed
> verification and publication. The existing `v0.1.0` tag points to an earlier commit
> than this RC branch; do not publish this branch's build as `v0.1.0` without resolving
> the source/tag mismatch. See
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

In future versions built from this change, the desktop launcher uses
`NatureAIWorker.exe`, a version-local copy of the verified Windows venv
Python launcher. This is **not a separately signed application binary**.
Use Windows Task Manager > **Details** to find `NatureAIWorker.exe`; the
Processes view may still show Python because the copied executable retains
Python's file-description metadata. The console title is also
`Nature AI Worker (Standalone)`. Native Windows startup, shutdown,
upgrade/rollback and uninstall require fresh testing on a new build.
The existing installed 0.1.0 RC is not modified by repository commits.

New Work and work conversations now have a `+` file picker. Successful
uploads go to the existing Standalone workspace and their filename is
added to the task instructions or follow-up message. No Platform Client
storage or API is used.

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

- **First installation:** verify the published SHA-256 of the installer file before
  installation. The embedded release manifest signature is also checked by the
  upgrader shipped in the same installer.
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

Two helpers stage signed Windows builds with the **official private signing key**, which stays on
the signing computer. Neither tags, pushes, uploads nor publishes anything, and neither assumes a
version: the version (and, for a candidate, the commit) is given explicitly and must match the
committed source, the release notes and the signed manifest. Every check runs before the
passphrase is requested.

- `scripts/windows_rc_stage.ps1 -Version X.Y.Z -Commit <full sha> [-Label rc1]` stages a **private
  release candidate** from an exact, untagged commit checked out in detached HEAD mode. It refuses
  if the official `vX.Y.Z` tag already exists. The committed `docs/RELEASE_NOTES_vX.Y.Z.md` must
  be final text; the "private release candidate" banner is added only to the copy signed into the
  candidate, so the same commit can later be tagged unchanged. `release_bundle.py verify` must
  confirm the signature, the official key and the repository, and must report the build as **not
  an official release** (no tag). The script writes `rc-evidence.json` with the commit, manifest
  SHA-256 and installer SHA-256.
- Both helpers verify **the payload Setup will install**, not a separate copy. Staging keeps only
  `payload.tar`. The check is `windows_installer.py verify-stage`, run via
  `Invoke-StagedPayloadVerification`:
  - `payload.tar` must match the SHA-256 in `build-info.json` and the `PayloadSha256` compiled into
    `defines.iss`;
  - it is then extracted into a new temporary folder. Only plain files and folders under
    `payload/` are accepted: no links, devices, absolute, drive, backslash, `.`/`..`, device-name
    or stream paths, and no duplicates. Each file is created exclusively and must resolve inside
    the folder. One unsafe member refuses the whole archive;
  - the manifest, runtime, `bootstrap.py` and trust-key file must match the build records, and
    nothing may look like a private key;
  - the extracted bundle goes through `release_bundle.py verify`.

  The temporary folder is always removed, and the run fails if it cannot be.
- `scripts/windows_official_release_stage.ps1 -Version X.Y.Z` builds from the exact `vX.Y.Z` tag
  (detached HEAD at the tag) and requires a fully verified official release before compiling.

Prerequisites: Windows, Git with the full history, Python 3.13 with the repository's pinned
dependencies, and Inno Setup 6 to produce the EXE (without ISCC only the installer files are
staged). Run from PowerShell on the **signing computer**, not in CI:

```powershell
git fetch origin
git checkout --detach <commit>
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps .
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows_rc_stage.ps1 -Version X.Y.Z -Commit <commit>
```

The candidate is staged in `dist\windows-rc-X.Y.Z-rc1` (official builds: `dist\windows-tagged-official-vX.Y.Z`);
an existing folder is never overwritten. Test upgrades from the previous release on a disposable
Windows account before any publication.

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
provenance, native acceptance and licensing checks.

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
- **Legal review:** see [Licensing review](#licensing-review) (Microsoft runtime terms, Inno
  Setup commercial-use request).
- **Code signing:** the installer is not Authenticode-signed. See
  [decision 0002](decisions/0002-windows-code-signing.md) (open: signed installer, or an explicitly
  unsigned one with documented limitations).
- **Privileged security tests:** 29 security tests need symbolic links or elevation and are
  skipped elsewhere, including CI. Run `scripts/windows_privileged_security_tests.py` on the
  dedicated test computer; it reports any skip as NOT RUN.
- **Native evidence on the current revision:** see VERIFICATION.md. Every result there names
  the tree it was built from.

## Licensing review

Reviewed 2026-10-28 from the shipped files themselves, and re-checked 2026-11-06 for v0.1.2. This
is not legal advice; the open items need the owner's decision.

The 2026-11-06 re-check used a staged v0.1.2 build and the pinned inputs:

| Item | Finding |
|---|---|
| Runtime | `ssl.OPENSSL_VERSION` from the shipped runtime is OpenSSL 3.5.9; SQLite 3.53.1 |
| `cryptography` 50.0.2 wheel | `_rust.pyd` statically links OpenSSL 4.0.3 |
| Microsoft files in the runtime | `vcruntime140.dll` and `vcruntime140_1.dll` |
| Inno Setup | installed compiler 6.7.3, as pinned; licence read from its installed `license.txt` |
| OpenSSL licence text | `runtime-licenses/LICENSE.openssl-3.txt` is byte-identical to upstream `LICENSE.txt` at the `openssl-3.5.9` and `openssl-4.0.3` tags (SHA-256 `7d5450cb…`) |

| Component | License | Status |
|---|---|---|
| Nature AI Worker | Apache-2.0 | LICENSE and NOTICE installed; the license is shown in Setup |
| Python packages (42 wheels) | permissive (MIT, BSD, Apache-2.0, PSF), certifi MPL-2.0 | every wheel's license files in `THIRD_PARTY_LICENSES.txt`; certifi unmodified, its source (`.py`) is in the wheel |
| OpenSSL 4.0.3 inside the `cryptography` wheel | Apache-2.0 | named in THIRD_PARTY.md; the Apache-2.0 text is in `THIRD_PARTY_LICENSES.txt` (the wheel ships cryptography's own Apache/BSD files); no NOTICE upstream, see 3 |
| CPython 3.12.15 | PSF-2.0 | `LICENSE.txt` reproduced |
| Runtime libraries: OpenSSL 3.5.9 (Apache-2.0), SQLite (public domain), libffi, expat (MIT), zlib, bzip2, liblzma, mpdecimal, libuuid, Tcl/Tk, Tix | permissive | texts reproduced from python-build-standalone's own license files; the stripped runtime omits them, so `runtime-licenses/` carries them |
| pip inside the runtime, and the pip wheel used for venvs | MIT, with vendored packages | license files reproduced |
| Microsoft Visual C++ runtime (`vcruntime140.dll`, `vcruntime140_1.dll` in the runtime) | Microsoft Distributable Code terms (in CPython's Windows `LICENSE.txt`) | **open**, see 1 |
| Inno Setup 6.7.3 code inside `Setup.exe` | Inno Setup License | notice kept in NOTICE; **open**, see 2 |

**Open legal issues:**

1. **Microsoft Distributable Code.** CPython's Windows `LICENSE.txt` says that whoever further
   distributes programs including Microsoft's Distributable Code "must require distributors and
   external end users to agree to terms that protect the Microsoft Distributable Code at least
   as much as Microsoft's own requirements". Setup shows only the Apache-2.0 license; the
   Microsoft terms are installed in `licenses\` but not presented for agreement. The owner
   should decide whether to present end-user terms that cover this, or confirm, with counsel,
   that the current notices suffice.

   The same text also says that redistributing the Windows build of the interpreter "complies
   with this agreement, provided that you do not":
   - alter Microsoft's notices;
   - use Microsoft's trademarks in program names or to suggest endorsement;
   - distribute the code for a non-Microsoft platform;
   - include it in malicious, deceptive or unlawful programs.

   The installer does none of these, but that does not settle the question. **Still open.**

   **Evidence (2026-11-06):**
   - `vcruntime140.dll` and `vcruntime140_1.dll` in the pinned runtime are Microsoft-signed
     (Authenticode status Valid) and unmodified, version 14.44.35211.0 (Visual Studio 2022
     toolset 14.44).
   - They are installed app-locally in each version's runtime folder; no VC++ Redistributable
     package is installed.
   - The runtime's `python.exe` and `python312.dll` are not signed.
   - Microsoft's own Visual Studio licence terms and redistribution list were **not** reviewed
     here.

   **Unresolved questions for counsel** (bundling a runtime is not assumed to permit
   redistribution):
   1. **Chain of rights.** The DLLs were redistributed by python-build-standalone (Astral), whose
      Visual Studio licence made them Distributable Code. Does that licence let downstream
      distributors such as NATURE AI redistribute them, or does NATURE AI need its own basis,
      for example its own Visual Studio licence, or shipping Microsoft's VC++ Redistributable
      package instead?
   2. **Whose statement is the proviso.** The "complies with this agreement, provided that you
      do not …" paragraph was written by the PSF for python.org's Windows builds. Does it apply
      to python-build-standalone's build, which reproduces CPython's `LICENSE.txt` unchanged?
   3. **End-user agreement.** Does "require distributors and external end users to agree to
      terms" require an affirmative agreement? Setup's licence page (`LicenseFile`, "I accept")
      currently shows only Apache-2.0. Or do the installed notices suffice?
   4. **Minimum terms.** If terms must be presented, what text protects the Distributable Code
      "at least as much as Microsoft's own requirements"? Must it go into Setup's licence page,
      and must the Worker's `NOTICE` or `THIRD_PARTY.md` pass it on?
   5. **Downstream redistributors.** Apache-2.0 lets anyone redistribute the installer. Must
      NATURE AI bind such distributors to the Microsoft terms, and how?
   6. **App-local deployment and servicing.** Microsoft documents app-local deployment of these
      runtime files. Is it within the applicable terms for this product? Who is responsible for
      shipping Microsoft security updates to them, given that Windows Update does not service
      app-local copies?
2. **Inno Setup commercial use.** The Inno Setup License (6.7.3, installed `license.txt`) grants
   use "for any purpose, including commercial applications". Its binding conditions are:
   - binary redistributions must retain the copyright notice and web-site addresses that are in
     place;
   - the origin must not be misrepresented;
   - modified versions must be marked as such.

   `Setup.exe` is unmodified Inno Setup output and NOTICE acknowledges Inno Setup, so the
   licence conditions are met. jrsoftware's ordering page separately *requests* that commercial
   users buy a licence. That is a request, not a licence condition. **Owner decision still open.**
3. **OpenSSL NOTICE: resolved, no NOTICE obligation.** Apache-2.0 §4(d) requires passing on a
   NOTICE file only if the work has one. The upstream source trees at both shipped versions
   (`openssl-3.5.9`, runtime; `openssl-4.0.3`, `cryptography` wheel) contain no `NOTICE`,
   `NOTICE.txt` or `NOTICE.md`; their root holds `LICENSE.txt`, `AUTHORS.md` and
   `ACKNOWLEDGEMENTS.md`. The Apache-2.0 licence text (§4(a)) is shipped, byte-identical to
   upstream. Optional improvement: label that text in `THIRD_PARTY_LICENSES.txt` as covering both
   OpenSSL copies, not only the runtime's.
