# Decision 0002: Authenticode code signing of the Windows installer

Status: **open owner decision. Neither option below is approved.** Recorded 2026-11-06 for v0.1.2.

## Context

A Windows release carries two independent kinds of integrity evidence. Neither is a substitute
for the other.

| Mechanism | What it covers | Who checks it | Status |
|---|---|---|---|
| **Ed25519 manifest signature** (`release.json.sig`, official key `64ecfc2da2ed12e9`) | The release bundle inside the installer: every wheel, the runtime, the notes | The Nature AI Worker upgrader, before anything is installed | In place ([RELEASES.md](../RELEASES.md)) |
| **SHA-256 of the installer** (`SHA256SUMS.txt`) | The downloaded `NatureAIWorker-Setup-X.Y.Z.exe` file | The user, by hand (`Get-FileHash`) | In place |
| **Authenticode** (X.509 code-signing certificate, PE signature) | The `.exe` itself, as Windows sees it | Windows: SmartScreen, Smart App Control, AppLocker/WDAC, antivirus | **Not in place** |

Windows evaluates only Authenticode. A SHA-256 checksum or an Ed25519 manifest signature does
not identify the publisher to Windows and does not change any SmartScreen or policy decision.

Today `packaging/windows/NatureAIWorker.iss` has no `SignTool` directive and no signing step
exists in `scripts/windows_rc_stage.ps1` or `scripts/windows_official_release_stage.ps1`. Unsigned PE files:
- `NatureAIWorker-Setup-X.Y.Z.exe`;
- the Inno Setup uninstaller;
- the bundled runtime's `python.exe` and its version-local copy `NatureAIWorker.exe` (python-build-standalone does not Authenticode-sign its builds).

## Option A: a correctly code-signed public installer

What would be signed:
- **required:** `NatureAIWorker-Setup-X.Y.Z.exe` and the uninstaller (Inno `SignedUninstaller=yes`);
- **recommended:** each version's `NatureAIWorker.exe` launcher, so Smart App Control and application-control policies see a known publisher for the program that actually runs. This needs a signing step inside the version build, because the launcher is created at install time from the runtime's `python.exe`.

What it needs (none of this exists yet):
1. **Certificate:** an OV or EV code-signing certificate issued to the legal entity that publishes releases (organization validation takes days to weeks).
   - CA/Browser Forum rules require the private key to be in a hardware token, an HSM or a cloud signing service, such as Microsoft's managed signing service or a CA's cloud HSM. It cannot be a file.
   - Custody is separate from the Ed25519 release key: different keys, different purposes.
2. **Timestamping:** an RFC 3161 time-stamping server (`/tr <url> /td sha256`), so signatures stay valid after the certificate expires.
3. **Pipeline changes:**
   - an Inno `SignTool=` definition (`signtool sign /fd sha256 /tr ... /td sha256 $f`) and `SignedUninstaller=yes`;
   - a `-SignTool` parameter in both stage scripts;
   - recording the installer SHA-256 **after** signing, since Authenticode changes the file's bytes and therefore its SHA-256;
   - verification with `signtool verify /pa /v` and `Get-AuthenticodeSignature` (status `Valid`, expected subject);
   - tests for all of this.
4. **Order:** build and Ed25519-sign the bundle (unchanged), compile, Authenticode-sign, verify, then hash for `SHA256SUMS.txt`. The Ed25519 signature inside the payload is not affected by Authenticode.

What it does **not** guarantee: SmartScreen reputation is still earned over downloads. A newly
signed file can show a warning for a while, with either OV or EV certificates. Do not promise "no warnings".

Release-notes text if Option A is chosen:

> The installer and its uninstaller are code-signed (Authenticode) by **<legal publisher name>**.
> Before running it, right-click the file → *Properties* → *Digital Signatures* and check that the
> signer is <legal publisher name> and the signature is OK; also compare its SHA-256 with
> `SHA256SUMS.txt`. Windows SmartScreen may still show a warning for a new release until it
> has built reputation.

## Option B: an explicitly unsigned installer with documented limitations

The installer and the programs it installs are not Authenticode-signed. Integrity is still
verifiable by hand (SHA-256) and by the upgrader (Ed25519), but Windows treats the publisher
as **unknown**.

Known limitations, to be stated to users:
- **SmartScreen:** "Windows protected your PC" on first run of the download; continuing needs *More info* → *Run anyway*.
- **Smart App Control (Windows 11)** in enforcement mode can block unsigned programs that have no reputation. That includes the installer and possibly the installed `NatureAIWorker.exe`. It cannot be overridden per file; the only way round it is to turn Smart App Control off, which cannot be turned back on without resetting Windows. Users must not be told to do that lightly.
- **Managed computers:** AppLocker, WDAC or endpoint-protection policies that allow only signed or approved software will block it. Administrators would have to allow it by file hash, per version.
- **Antivirus heuristics** flag unsigned installers more often.
- There is **no UAC prompt** in any case: installation is per user, `PrivilegesRequired=lowest`.

Mitigations that remain:
- `SHA256SUMS.txt` with the release;
- the Ed25519-signed manifest, checkable with `nature-worker-upgrade check`;
- download only from the GitHub Release of the tag;
- the source commit of the manifest equals the tag.

Release-notes text if Option B is chosen:

> **This installer is not code-signed (no Authenticode signature).** Windows SmartScreen will
> show "Windows protected your PC" and the publisher as unknown. Before choosing *More info* →
> *Run anyway*, download the installer only from the GitHub Release of the `vX.Y.Z` tag and
> check that `Get-FileHash .\NatureAIWorker-Setup-X.Y.Z.exe -Algorithm SHA256` equals the value
> in `SHA256SUMS.txt`. Windows 11 Smart App Control and managed-computer policies (AppLocker,
> WDAC) may block unsigned programs; on managed computers ask your administrator. The release
> bundle inside the installer is verified with the official Ed25519 release key before anything
> is installed, but that check does not replace a Windows code signature.

## Questions for the owner

1. Which option applies to v0.1.2?
2. **For Option A:** the legal publisher name, the CA, the key storage (token, HSM or cloud signing), the time-stamping service, and whether the version-local `NatureAIWorker.exe` is signed too.
3. **For Option B:** approval of the exact limitation text above, its placement (release notes, README download section, SUPPORT.md), and whether managed-computer customers are supported.

## Consequences

- **Acceptance:** whichever option is chosen, Windows acceptance testing records:
  - the SmartScreen behaviour on a downloaded copy (Mark of the Web), on a computer with Smart App Control in evaluation or enforcement mode where available;
  - `Get-AuthenticodeSignature` on the installer and on `NatureAIWorker.exe`.
- **Option A:** the RC must be rebuilt and re-signed after the pipeline change. The SHA-256 values from an unsigned build do not carry over.
