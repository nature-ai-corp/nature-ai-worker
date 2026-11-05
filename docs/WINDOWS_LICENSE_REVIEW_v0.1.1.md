# Windows v0.1.1 Binary Distribution License Review (Internal Draft)

**Status:** Preliminary engineering review, not legal advice or permission to publish. **Owner clarification (2026-11-05):** Apache-2.0 remains in place. Nature AI Corp may charge for **optional OEM services and separately licensed value**, but no charge is mandatory for redistributing Apache-2.0 Worker, and the project prefers upstream open-source materials without mandatory paid redistribution fees. No payment, pricing model, or license change has been approved.

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

## Actual packaging/code inspection (2026-11-05)

Inspected the release branch's pinned inputs and installer source rather than inferring from generic licensing notes:

- `packaging/windows/runtime.json` pins the **exact** `python-build-standalone` Windows x64 CPython **3.12.15**, release `20261009`, `install_only_stripped` archive, SHA-256 `11ed9f8a6ac64fb1b5a55e40e305a6744a3f77a70863c8f8197a6e4349abc64a`.
- `scripts/windows_installer.py::license_texts()` reads `python/LICENSE.txt` **from that runtime archive** and appends files under `packaging/windows/runtime-licenses/`, plus license material found inside the pinned dependency wheels. It stages a generated `licenses/THIRD_PARTY_LICENSES.txt` alongside `LICENSE.txt`, `NOTICE.txt`, and `THIRD_PARTY.md`.
- `packaging/windows/NatureAIWorker.iss` has **only** `LicenseFile=licenses\\LICENSE.txt` (the Worker's Apache-2.0 license). It copies `licenses/*` into the app directory and provides a Start-menu `Licenses` shortcut. No separate Microsoft terms acceptance page or third-party `InfoBeforeFile`/license gate is configured in the inspected source.
- The applicable upstream **CPython 3.12.15** additional conditions at https://github.com/python/cpython/blob/v3.12.15/PC/crtlicense.txt explicitly state that distributors must require downstream distributors and external end users to agree to terms protective of Microsoft Distributable Code. Microsoft's current Visual C++ redistribution guide https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files says redistribution is subject to software license terms and limited to licensed Visual Studio users; **that guide alone does not establish whether the specific third-party prebuilt python-build-standalone artifact grants this publisher sufficient downstream rights**.
- The installer already **includes the applicable text** in generated third-party licenses, but **agreement/acceptance and runtime distribution entitlement remain unverified**. Do not mark compliant or assume adding a check box alone cures entitlement.
- **Next evidence required:** extract the **exact staged** `python/LICENSE.txt`, identify bundled Microsoft runtime file versions/provenance, inspect the installer license screen and its silent-install behavior, and determine appropriate protective downstream terms with rights/legal review. If necessary, change installer wording/acceptance and retest with an actual staged build; no code change has yet been made.
- OpenSSL 3 is Apache-2.0 under its official project licensing page https://openssl-library.org/source/license/index.html. The pinned Windows OpenSSL license text is included, but the exact runtime/OpenSSL third-party notice inventory remains unverified.

**Scope of review:** repository source and upstream policy documents; **not** a binary-level disassembly/extraction or a legal sign-off. This review does not establish any per-copy royalty or mandatory Microsoft fee.

## Actual Windows RC staging evidence (owner PowerShell, 2026-11-05)

The maintainer inspected the **existing** staging directory at `C:\\Users\\aiuser\\nature-ai-worker-release\\dist\\windows-rc-stage-0.1.1` on the AI Worker Computer (read-only; no rebuild, no key access).

| Staged path | Measured bytes |
| --- | ---: |
| `licenses/LICENSE.txt` | 11,560 |
| `licenses/NOTICE.txt` | 1,320 |
| `licenses/THIRD_PARTY.md` | 5,856 |
| `licenses/THIRD_PARTY_LICENSES.txt` | 378,108 |

`THIRD_PARTY_LICENSES.txt` includes `Additional Conditions for this Windows binary build` at line 298; Microsoft Distributable Code language at lines 301–302; and the downstream-distributor / external-end-user requirement at lines 309–310. A search showed no separate Inno Setup informational or third-party license page: the staged `NatureAIWorker.iss` has `LicenseFile=licenses\\LICENSE.txt` at line 42 and no `InfoBeforeFile=` / `InfoAfterFile=` entries.

**Evidence-backed verdict:** the Microsoft restrictions **are included in the actual staged license payload**. Whether current installer presentation constitutes the required downstream contractual agreement and whether this exact CPython distribution grants sufficient Microsoft runtime redistribution rights remain **open**. This confirms staging files, **not** byte-for-byte inspection of the compiled Setup EXE or the actual click-through UI. No recommendation to replace Python or buy a license without further legal/source verification.

## Microsoft Windows runtime redistribution

The project-specific review in `docs/WINDOWS_STANDALONE_INSTALLER.md` notes that the pinned CPython Windows `LICENSE.txt` includes special terms concerning Microsoft Distributable Code. The installer currently displays Apache-2.0 as its setup license and separately installs the bundled third-party license texts. It has **not yet been established** that this satisfies the Microsoft-specific condition requiring protective terms for downstream distributors and external users.

**Primary-source confirmation (2026-11-05):** the [CPython Windows additional conditions](https://github.com/python/cpython/blob/main/PC/crtlicense.txt) expressly require downstream distributors and external end users to accept protective terms for the Microsoft Distributable Code. The [Microsoft EWDK redistribution terms](https://learn.microsoft.com/en-us/legal/windows-sdk/license-terms-ewdk) contain a similar requirement, but that particular EWDK agreement is **not automatically the specific governing license for the pinned python-build-standalone runtime**; verify its actual runtime provenance and terms. **Before official distribution:** check the exact pinned runtime license text and obtain a release-owner/legal determination as to whether a supplemental installer terms page or other affirmative notice is necessary. If packaging changes are needed, implement and retest rather than rebranding the whole source code license.

## Inno Setup commercial-use fees

The [Inno Setup order and licensing FAQ](https://jrsoftware.org/isorder.php) asks commercial users to purchase licenses, but states that they are **not strictly required**, and confirms organizations may defer a voluntary purchase until their installers are ready for production. This is a request, not an identified per-download royalty or compulsory distribution fee. Inno Setup fees concern payments **by the publisher to the installer vendor**, not OEM licensing revenue. This is separate from the owner's intended licensing business model. Do not represent Inno Setup purchase as legally mandatory.

No optional tool purchase is authorized. This license review does not determine the separate commercial redistribution policy.

## OpenSSL NOTICE verification

OpenSSL 3 is Apache-2.0 licensed. The bundled runtime license set includes the OpenSSL license, and the cryptography wheel may also link OpenSSL. **Primary-source confirmation (2026-11-05):** [OpenSSL project licensing](https://openssl-library.org/source/license/index.html) confirms OpenSSL 3.x uses Apache-2.0, and the exact [OpenSSL 3.0.18 LICENSE.txt](https://raw.githubusercontent.com/openssl/openssl/openssl-3.0.18/LICENSE.txt) is available. A direct check for `NOTICE` at this tag returned 404, but that alone **does not prove no other upstream notice or attribution file is applicable**, especially across different runtime/cryptography-linked OpenSSL versions. Compare the exact included build and shipped library versions with their upstream NOTICE/attribution materials before release. If needed, append the required notice to `THIRD_PARTY_LICENSES.txt` or other suitable binary distribution notice location and rebuild.

## Artifact-level follow-up before official GitHub Release

- [ ] Review `packaging/windows/runtime.json` pinned CPython license, Microsoft additional conditions, and actual installer legal prompt.
- [ ] Verify OpenSSL NOTICE requirements against the exact shipped dependency/runtime builds.
- [ ] Confirm `LICENSE`, `NOTICE`, `THIRD_PARTY.md`, and generated `THIRD_PARTY_LICENSES.txt` are included in final installer and accessible after install.
- [ ] If legal/notice changes are needed, review implementation and run targeted packaging checks plus real Windows installation.
- [ ] Decide whether to acquire the optional Inno Setup commercial-use license (no automatic purchase).
- [ ] Record approved final release source commit/tag, package and manifest SHA-256, download destination, and release-owner sign-off.
- [ ] Keep any test/model secrets, signing private keys and user backups off public GitHub releases.

**Commercial policy decided:** preserve Apache-2.0 and offer paid OEM support, customization, co-branding permissions or separate proprietary components only as optional value; no fee is compulsory for Apache-permitted incorporation or redistribution. Prefer third-party open-source dependencies whose licenses impose no mandatory distribution royalties. Inno Setup's optional commercial-use purchase is not approved or required. See `docs/COMMERCIAL_DISTRIBUTION_POLICY_DRAFT.md`. Microsoft runtime redistribution terms and OpenSSL notices remain independent compliance checks.
