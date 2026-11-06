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
