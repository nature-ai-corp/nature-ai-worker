# v0.1.1 Windows-first publication checklist (maintainer-only)

**State:** publication pending. This is a procedure, not permission to publish, change visibility or purchase services.

## Verified private-candidate evidence

- Source commit validated on AI Worker Computer: `dfbf8f059760b7fdc26a6b487e15891c6b602f60`; clean checkout and **624 passed, 64 skipped, 0 failed**.
- Official Ed25519 key ID `64ecfc2da2ed12e9`. Candidate manifest signature **PASS**; 45 signed files verified byte length and SHA-256 **PASS**.
- Private Windows installer `NatureAIWorker-Setup-0.1.1.exe` SHA-256 `D281097427107BF95796B15FA62D075D459F83A5DD402A6D9922B84124416272`.
- Fresh native Windows install, browser first-run, `NatureAIWorker.exe` identity in Task Manager, and normal process shutdown **PASS**, using the exact private-candidate installer in a separate WorkerTest account.
- Earlier no-model Windows upgrade/rollback kit: 12 PASS, 0 FAIL, 0 ERROR, 1 NOT EXECUTED; cleanup 2 PASS, 0 FAIL, 0 ERROR. OpenAI file attachment/read passed in a separate real-model check.
- GitHub Actions are not claimed to pass. Existing `v0.1.0` tag must not be changed.
- The **private-candidate** installer is **not** an official `v0.1.1` artifact: it was built before the version-matching Git tag existed. Do not publish it merely by renaming it.
- Inno Setup 6.7.3 currently reports `Non-commercial use only`. If the owner plans commercial installer distribution, clarify installer-tool licensing before that use. Separate third-party notice review is recorded without changing Apache-2.0.

## Public / private boundary — mandatory before publication

**Public/open source:** `nature-ai-worker` repository source authorized for release, Standalone runtime/UI, general Worker tools, documented Platform Client **client-side** implementation, and the **owner-approved S–C Client Interface v1.3 contract** (`src/nature_worker/contracts/client-interface-v1.openapi.json`). Apache-2.0 applies to code intentionally released here. Do not treat the S–C contract approval as permission to publish Platform Server internals.

**Private / must never be copied to public releases:** NATURE AI Platform server/backend code, proprietary orchestration, internal agent prompts and business knowledge, Validator/Coach private implementation, deployment configuration, credentials/API keys, release-signing private keys or passphrases, actual enterprise/user records, internal log/transcript contents, production addresses/hostnames or private network details, proprietary commercial/OEM agreements, internal test reports containing sensitive details. Keep the separate private Platform repository private.

**Public Worker vs private editions:** The current open-source Worker contains Standalone and an authorized Platform Client (S–C) mode in one repository. A separate proprietary/internal Worker distribution is a **deferred product plan**, not part of v0.1.1, and should not be implied to exist as a shipped package. If it is implemented later, its proprietary code, configuration, build products and licenses must live in separately controlled private repositories/pipelines and **never** be included in the public source or Windows installer. Do not confuse a private **release candidate** of the public Worker with a proprietary **private edition**: the former is only a testing stage for publicly licensable source.

**Separate categories of files and artifact handling:**

| Material | Destination / treatment |
| --- | --- |
| Reviewed Worker source, public README, LICENSE, NOTICE, applicable client interface contract, public user release notes | Public open-source repo / official release where approved |
| Signed public release bundles, final installer, public SHA256 checksums | GitHub Release **only after** source-tag-signature verification and explicit authorization |
| Internal RC installers and acceptance kits (especially broken-upgrade fixtures), raw Windows logs, test user backup folders, internal validation narratives | Private test storage; do **not** attach to public Release |
| Private key PEM, passphrase, API keys, `.env`, enterprise config and data | Controlled local/private storage only; **never** git or release assets |
| Platform Server, internal business rules, server APIs not covered by explicit public contract approval | Private Platform repository only |

**Pre-publication gates (must all pass and be recorded):**

- [ ] Determine scope of visibility change: only the reviewed Worker repository; **never** make NATURE AI Platform/Validator/Coach repositories public as a side effect.
- [ ] Review the exact Git **tag commit and all history reachable from the public refs**, not just current files. Run `python scripts/repo_checks.py --history` in a full clean clone, then manually review findings and the candidate release archive. Verify historical rewrite/backup refs or unpublished sensitive commits are not pushed.
- [ ] Explicitly verify that the public Client Interface contract contains only the owner's approved v1.3 interface material (no live tenant information, credentials or Platform Server implementation). Remote snapshot: 23 paths / 28 operations / 40 schemas, no OpenAPI `servers` field, no matched private network/tailnet host or common private-key/token prefixes in the contract text; **does not replace** source-to-source comparison.
- [ ] Inspect public release notes, `NOTICE`, support links, installer metadata, bundled license text, `release.json`, `build-info.json` (if distributed), and installer `payload.tar` contents for disclosures. **Do not** blindly upload `dist/`, private test reports or logs.
- [ ] Check artifact lists/archives against a **positive allowlist**, rather than assuming everything in the private checkout is publishable. Ensure no private keys, passwords or confidential test data inside wheels, install payload or build outputs.
- [ ] Keep the official Ed25519 private key only on the controlled signing computer; only the official **public** verification key may ship.
- [ ] Confirm Standalone and S–C modes do not interchange stored data, sessions, settings, credentials or logs, and that no in-place S–C to Standalone conversion is offered.
- [ ] Distinguish product data licensing: public Worker code is Apache-2.0; the enterprise Platform Server, nonpublic OEM services/components and enterprise data are **not** put under Apache-2.0 merely by connecting them through an approved interface.
- [ ] Obtain separate, explicit owner approval for public repo visibility and publication, after reviewing the above evidence. Publishing a GitHub Release on a **private** repository does not make it publicly downloadable.

## Public-boundary audit snapshot (2026-11-05/06)

**Partial remote inspection; NOT a clearance to make the repository public.** Inspected the complete GitHub recursive tree for both the previously validated source commit (`dfbf8f0`, 231 entries) and documentation PR revision (`1e9d6d5`, 232 entries), plus the existing `repo_checks.py`, `.env.example`, the contract provenance and selected test fixture/source documents. The tree responses were `truncated=false`.

- No tracked private-key PEM, actual `.env`, tracked database or install EXE was apparent from those *file paths*. This is only a path-level observation, not a full-text or historical secret-scan PASS.
- `src/nature_worker/contracts/client-interface-v1.provenance.json` explicitly records `committed_contract_compared: false`. **BLOCKER:** compare the checked-in v1.3 public contract against the Platform's authoritative committed contract using `python -I scripts/client_interface_contract.py compare --ignore-descriptions <committed.json> src/nature_worker/contracts/client-interface-v1.openapi.json` on an authorized computer. Review any differences before publishing; do not export other Platform files.
- `scripts/repo_checks.py` has a real coverage gap: `check_history()` calls `scan_text(..., tests=True)` and then only collects `secret` findings. Therefore `--history` does **not** surface the same historical private-address/tailnet-hostname and confidentiality-marker checks as the current-tree scan. **BLOCKER:** enhance this history coverage (or independently inspect all reachable history) and run it against a fresh full clone with all refs intended for publication. Review findings without exposing private values in public PR comments.
- `tests/data/windows_service_profile_e2e.json` contains a structure/metadata fixture with Windows service-profile path names and autogenerated identifiers, not captured file content. This is not itself proof of credential disclosure, but it deserves manual review for enterprise-specific identifiers before public publication.
- The approved public interface is a **client interface only**. Existence of `src/nature_worker/platform_client/` does not by itself authorize the disclosure of any Platform Server implementation; verify contents and permitted provenance.
- Confirm public source/docs do not include misleading stale engineering instructions or internal-only artifacts. The private RC installer SHA-256 is for internal evidence **only**, not the SHA-256 of the yet-to-be-built official installer.
- Finish bundle and published-ref inspection locally: neither the release candidate installer nor complete reachable Git object history was available to this remote review session. **No claim** of `repo_checks.py --history` execution or full wheel/install-payload inspection.

**Release gate:** Public visibility, public tag/release assets and published downloads remain blocked until the two explicit BLOCKER findings above are resolved and the remaining positive-allowlist reviews are completed. Record PASS/FAIL/NOT EXECUTED evidence with the exact reviewed commit, without publishing sensitive findings.

## Evidence update — verified on owner's Windows computer (2026-11-05 21:20 EST)

Owner-provided PowerShell output for commit `9f15f9cb3d3b10a258e062ebda54efe6dcd2b491` on `fix/repo-history-sensitive-content-audit`:

- `WORKTREE : PASS`
- `REGRESSION : PASS`
- `HISTORY AUDIT : PASS for all refs in this local clone`
- `CONTRACT COMPARE : NOT EXECUTED (authoritative committed Platform contract not supplied)`
- Raw reports kept privately at `dist/private-release-preflight-20261105-212038` (not included in public source/release assets).

**Interpretation:** the history-scan implementation is validated against the owner's local refs, with zero findings after narrowing the audited legacy RC warning and two historical test-example false positives. Recheck after final merges and fetch all refs intended for public publication. The authoritative committed Platform contract comparison remains a separate mandatory blocker, not a PASS.

## Read-only Platform contract comparison

The security PR provides `scripts/windows_platform_contract_compare.ps1`. Run it **only on an authorized Windows computer with local access to both the Worker checkout and the private Platform Git checkout**, supplying `-PlatformRepo "<private-platform-repo-path>"` and optionally `-PlatformRef "<verified-commit-sha>"`. The script reads **only** `docs/client-interface-v1.openapi.json` from that Platform Git commit, temporarily stores the comparison bytes under the Windows user TEMP directory (not inside Worker), compares after stripping descriptions, reports only booleans/counts and the Platform commit, and cleans up temporary files. Never share raw contract diff output or private Platform paths. If the authoritative contract is absent, the comparison is NOT EXECUTED, not a PASS. Review the result against the approved publicly distributable interface scope before merging or publishing.

## Accelerated controlled Windows preflight

On AI Worker Computer, fetch the latest `fix/repo-history-sensitive-content-audit` branch, then run `powershell -ExecutionPolicy Bypass -File .\\scripts\\windows_release_preflight.ps1`. It executes the new regression tests and repository checks including `--history`, keeps potentially sensitive raw output in a local gitignored `dist/private-release-preflight-*` folder, and prints a safe high-level summary. For a contract comparison, provide an **authorized local path** to the Platform's committed v1.3 JSON with `-CommittedPlatformContract`; this script never copies it into the Worker repository. **Not executed** until run on the owner's Windows computer. A green scan covers refs in the actual local clone; review all remote/publication refs before public release. A comparison is **not** verified merely because the optional path was omitted.

## Release preparation

1. Confirm `docs/RELEASE_NOTES_v0.1.1.md` is the finished public-facing note, with no claims for unexecuted model scenarios. Keep internal checklists separate from user release text.
2. Choose and review the **final release commit** (which may differ from the tested `dfbf8f0` because release-note edits become commits). Record its full SHA and validate package metadata version `0.1.1` in both `pyproject.toml` and `src/nature_worker/__init__.py`.
3. Perform final relevant local tests and record any skips/limitations. Keep repository worktree clean.
4. With explicit owner authorization, create an **immutable new** `v0.1.1` tag at that exact commit. Do not move any existing tag. Inspect `git rev-parse 'v0.1.1^{commit}'` and `git describe --tags --exact-match HEAD`.
5. On the controlled AI Worker signing computer, build from **the tagged commit**, using official key ID `64ecfc2da2ed12e9`, official user-facing release notes, pinned Windows Runtime and wheel hashes. **Do not use `scripts/windows_rc_stage.ps1` unmodified for the official build**: it explicitly hardcodes `docs/RELEASE_NOTES_v0.1.1-rc.md`, marks output PRIVATE RC and is intended for internal candidate staging. Use or prepare a separate approved official-release command/path. Keep private key and passphrase local.
6. Confirm the output manifest has `version = 0.1.1`, `source.tag = v0.1.1`, `source.commit =` the tag target SHA, official signer ID, and `upgrade_from.min = 0.1.0`.
7. Verify the final generated release bundle via `scripts/release_bundle.py verify --bundle <bundle-directory> --repo <clean-clone-with-v0.1.1-tag>` or equivalent project verifier, plus installer SHA-256. Note that signature/hash checks **alone** do not establish tag provenance.
8. Execute a targeted native Windows installer verification of the new **exact** final binary: clean per-user installation, startup, process identity and exit; confirm 0.1.0 upgrade if final installer behavior differs from accepted private RC.
9. Obtain explicit owner approval for public release and, if applicable, repository visibility. Ensure source publication does not expose private Platform implementation, credentials, sensitive contract files or keys. Publish only reviewed assets, notes, checksums and their matching tag.
10. Confirm the published assets and tag match the locally verified release, then record full commit, checksums, and GitHub release URL.

## Non-goals

No requirement to buy Microsoft Artifact Signing or GitHub Actions. No changes to the Apache-2.0 license or compulsory royalty for open-source redistribution. Optional commercial OEM services remain separate. No actual release, tag, visibility change, or installation is performed by this document.
