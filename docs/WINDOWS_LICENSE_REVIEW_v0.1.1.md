# Windows v0.1.1 Binary Distribution License Review (Internal Draft)

**Status:** Preliminary engineering review, not legal advice or permission to publish. **Owner clarification (2026-11-05):** the business goal is to **charge third-party distributors/OEMs who embed or redistribute Nature AI Worker inside their own commercial products**. This is revenue to NATURE AI CORP, **not** a statement that NATURE AI CORP wishes to buy software distribution tools. Existing Apache-2.0 recipients may already redistribute commercially without paying; do not imply these rights can be retroactively revoked. No purchase, pricing model, or license change has been approved.

## Inventory of distributed materials

The Windows Standalone installer contains CPython 3.12.15 (python-build-standalone), 42 dependency wheels plus the Nature AI Worker wheel, and an Inno Setup installer wrapper. The installer installs `LICENSE.txt`, `NOTICE.txt`, `THIRD_PARTY.md`, and generated `THIRD_PARTY_LICENSES.txt`.

| Component | Recorded terms | Status |
| --- | --- | --- |
| Nature AI Worker | Apache-2.0, LICENSE, NOTICE and source SPDX lines | Documented |
| Dependency wheels | Permissive license set; unmodified `certifi` uses MPL-2.0 | Documented; recheck final binary |
| CPython and bundled runtime libraries | PSF and bundled third-party licenses reproduced | Documented; recheck final binary |
| Microsoft runtime redistributable code in CPython Windows build | Special downstream distribution/end-user terms in CPython Windows license | **OPEN: confirm compliant end-user terms** |
| Inno Setup engine | Inno Setup license, attribution in NOTICE | **Review complete on fee distinction; no mandatory purchase identified** |
| OpenSSL 3 | Apache-2.0 license text included; any upstream NOTICE file still needs source verification | **OPEN: notice review** |

## Microsoft Windows runtime redistribution

The project-specific review in `docs/WINDOWS_STANDALONE_INSTALLER.md` notes that the pinned CPython Windows `LICENSE.txt` includes special terms concerning Microsoft Distributable Code. The installer currently displays Apache-2.0 as its setup license and separately installs the bundled third-party license texts. It has **not yet been established** that this satisfies the Microsoft-specific condition requiring protective terms for downstream distributors and external users.

**Before official distribution:** check the exact pinned runtime license text and obtain a release-owner/legal determination as to whether a supplemental installer terms page or other affirmative notice is necessary. If packaging changes are needed, implement and retest rather than rebranding the whole source code license.

## Inno Setup commercial-use fees

The [Inno Setup order and licensing FAQ](https://jrsoftware.org/isorder.php) asks commercial users to purchase licenses, but states that they are **not strictly required**. This is a request, not an identified per-download royalty or compulsory distribution fee. Inno Setup fees concern payments **by the publisher to the installer vendor**, not OEM licensing revenue. This is separate from the owner's intended licensing business model. Do not represent Inno Setup purchase as legally mandatory.

No optional tool purchase is authorized. This license review does not determine the separate commercial redistribution policy.

## OpenSSL NOTICE verification

OpenSSL 3 is Apache-2.0 licensed. The bundled runtime license set includes the OpenSSL license, and the cryptography wheel may also link OpenSSL. Compare the exact included build and shipped library versions with their upstream NOTICE/attribution materials before release. If needed, append the required notice to `THIRD_PARTY_LICENSES.txt` or other suitable binary distribution notice location and rebuild.

## Artifact-level follow-up before official GitHub Release

- [ ] Review `packaging/windows/runtime.json` pinned CPython license, Microsoft additional conditions, and actual installer legal prompt.
- [ ] Verify OpenSSL NOTICE requirements against the exact shipped dependency/runtime builds.
- [ ] Confirm `LICENSE`, `NOTICE`, `THIRD_PARTY.md`, and generated `THIRD_PARTY_LICENSES.txt` are included in final installer and accessible after install.
- [ ] If legal/notice changes are needed, review implementation and run targeted packaging checks plus real Windows installation.
- [ ] Decide whether to acquire the optional Inno Setup commercial-use license (no automatic purchase).
- [ ] Record approved final release source commit/tag, package and manifest SHA-256, download destination, and release-owner sign-off.
- [ ] Keep any test/model secrets, signing private keys and user backups off public GitHub releases.

**Commercial distribution policy gate:** before official public release, owners should decide between (a) keeping Apache-2.0 and offering optional paid OEM/commercial services or additional proprietary components, or (b) prospective dual-licensing/source-available terms that require a commercial agreement for third-party product embedding and redistribution. Under (b) ordinary use can remain free, but the restricted source would **not** be OSI open source. Copyright ownership, third-party dependencies, contributor rights and already-granted Apache-2.0 rights need legal review. Do not rewrite existing LICENSE/NOTICE without explicit owner decision; see `docs/COMMERCIAL_DISTRIBUTION_POLICY_DRAFT.md`.
