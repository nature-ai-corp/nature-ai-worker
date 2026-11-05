# Nature AI Worker v0.1.1 — Release Notes (Draft)

> **DRAFT — NOT PUBLISHED.** This document is the candidate user-facing release note for Windows-first Standalone v0.1.1. No final `v0.1.1` tag, official release bundle or GitHub Release has been published. Final download links, release SHA-256 and provenance must be inserted **after** building and validating the release from its final tag/commit.

## Highlights

- **Attach workspace files to work.** New Work and existing work conversations now offer a `+` attachment picker. Uploaded files use the existing Standalone workspace file API and their references become part of the work request.
- **Recognizable Windows launcher.** The Standalone desktop launcher runs as `NatureAIWorker.exe` (a version-local interpreter copy) and uses the console title `Nature AI Worker (Standalone)`.
- **In-place upgrade from 0.1.0.** The Windows 0.1.1 installer is designed to upgrade existing 0.1.0 Standalone installations without clearing settings, work history or workspace files.
- **Guarded upgrade and recovery.** Signed bundle verification, explicit upgrade confirmation for unattended installs, edition isolation, automatic rollback after a failed upgrade, and retaining user data on normal uninstall are preserved.

## Windows validation performed

The maintainer tested a private Windows release candidate on separate Windows computers and a dedicated, disposable local Windows account:

| Check | Result |
| --- | --- |
| Full local Python suite on the validated release-line revision | **622 passed, 64 skipped, 0 failed** |
| Standalone fresh install, named process and local app startup | **PASS** |
| OpenAI `gpt-4.1-mini` task with `+` upload and real file read | **PASS** |
| Real installed 0.1.0 to 0.1.1 in-place upgrade; original workspace file and past work retained | **PASS** |
| Isolated installer acceptance: install, upgrade, schema migration, session retention, failed-upgrade automatic rollback, manual rollback, downgrade/refusal, uninstall and data-preserving reinstall | **12 PASS, 0 FAIL, 0 ERROR** |
| Final isolated uninstall and explicit data cleanup | **2 PASS, 0 FAIL, 0 ERROR** |
| Ollama scenario within isolated Windows installer acceptance | **NOT EXECUTED** (no Ollama endpoint supplied) |
| DeepSeek scenario within isolated Windows cleanup acceptance | **NOT EXECUTED** (no DeepSeek configuration supplied) |
| GitHub-hosted CI on latest PRs | **NOT EXECUTED** (organization Actions billing/budget prevented jobs from starting); local tests and native Windows evidence are distinct |

Evidence references: isolated Windows reports `acceptance-auto-20261105-161025.md` and `acceptance-finish-20261105-161638.md`, retained by the maintainer. These reports and private user data are not attached to this public-facing document.

## Requirements and upgrade notes

- Windows 10 build 17134 or later or a supported Windows 11 version, x64, for the bundled Windows Standalone installer. Installation is per user and does not require administrator privileges.
- No system Python, Git, WSL or Docker is needed to use the Windows installer.
- The user selects a supported AI provider or local model and performs initial configuration. AI provider usage may involve charges or separate account requirements.
- Close or finish active work before upgrading. Normal uninstall retains the separately stored data and workspace; data deletion is a separate, explicitly confirmed action.
- Standalone and Platform Client are distinct modes with separate data and authorization. An installed Platform Client must not be converted into Standalone in place.

## Download verification

**Pending official release.** The final download filename, SHA-256 digest, release tag and matching source commit will be recorded here only after the final package is built and its signature, integrity and provenance have been verified.

## Open-source license

Nature AI Worker is licensed under Apache License 2.0. The installer includes license and attribution files for the shipped Python runtime and third-party libraries. See `LICENSE`, `NOTICE`, `THIRD_PARTY.md` and the installed `licenses` directory.

## Publication checklist (maintainer-only, remove before publishing)

- [ ] **Owner licensing decision:** existing Apache-2.0 permits free commercial redistribution; compulsory OEM/product-embedding fees would require a different prospective licensing model (not OSI open source) or a separately valuable proprietary OEM offering. See `docs/COMMERCIAL_DISTRIBUTION_POLICY_DRAFT.md`. Do not publish under conflicting terms.
- [ ] Confirm Windows binary redistribution terms, including Microsoft distributable runtime terms and OpenSSL notice review (see `docs/WINDOWS_LICENSE_REVIEW_v0.1.1.md`).
- [ ] Review source copyright/ownership notices.
- [ ] Select exact final release commit; rerun applicable tests after the final release-note changes.
- [ ] Create immutable `v0.1.1` tag at the verified manifest source commit (never move `v0.1.0`).
- [ ] Rebuild official Ed25519-signed release bundle and installer against the chosen source/tag; verify manifest signature, file hashes and version provenance.
- [ ] Record final installer SHA-256 and working download links. Do not use the old private RC digest as a final public release artifact.
- [ ] Explicitly approve repository visibility and GitHub Release publication.

