# Open-source rights protection

This page records how the Nature AI Worker repository protects the project's copyright, names and releases, and what it deliberately does **not** restrict. It is a project policy record, not legal advice. Anything marked **OWNER DECISION** needs the maintainers (and, where noted, legal counsel) to decide.

## 1. What is protected, and by what

| Asset | Protection | Where |
|---|---|---|
| Source code | Copyright of its authors, licensed to everyone under Apache-2.0 | `LICENSE` (unmodified Apache-2.0 text, SHA-256 `cfc7749b…3d30`, checked in CI), `NOTICE`, SPDX headers in every source file |
| Attribution | Apache-2.0 §4: redistributions must keep LICENSE, NOTICE and copyright notices, and mark modified files | `NOTICE`, SPDX headers; `NOTICE` is included in the wheel and the Docker image |
| Names "NATURE AI", "Nature AI Worker", the "N" logo | Not licensed by Apache-2.0 (§6); usage guidelines | [TRADEMARKS.md](../TRADEMARKS.md) |
| Official releases | Ed25519-signed manifests; the upgrader reports anything else as not official | [RELEASES.md](RELEASES.md) |
| Contributions | Inbound = outbound Apache-2.0 (§5) plus DCO sign-off | [CONTRIBUTING.md](../CONTRIBUTING.md) |
| Proprietary Platform code, credentials, internal infrastructure | Never committed; CI checks | `scripts/repo_checks.py`, CONTRIBUTING |

## 2. What Apache-2.0 permits, and what this project therefore does not restrict

Copyright, trademark and release-signing protections are **not** restrictions on using the software. Apache-2.0 grants every recipient a perpetual, worldwide, royalty-free right to use, reproduce, modify, distribute, sublicense and sell the Work, and a patent license from each contributor (§2, §3). The project does **not**, and under Apache-2.0 cannot, require or forbid any of the following:

- use for commercial purposes, by competitors, or in any field of use;
- forks, including hostile or competing forks;
- modified versions, including closed-source products built on this code;
- redistribution, free or paid, of official or modified builds;
- running the Worker without the NATURE AI Platform, or against another backend;
- payment, registration, or prior permission for any of the above.

What the project **can** require comes from the license itself and from law outside the license:

- keeping the LICENSE, NOTICE and copyright notices, and marking changed files (§4), which is a license condition;
- not using the project's names and logo to identify a different product or to imply endorsement (§6 and trademark/unfair-competition law, see [TRADEMARKS.md](../TRADEMARKS.md));
- not presenting a build as an official release. This is a factual matter: official releases are identified by signature, see [RELEASES.md](RELEASES.md).

Release signing restricts nothing. It lets users tell an official build from an unofficial one, and an installation accepts only what its operator trusts.

## 3. Audit of this repository (2026-10-26)

| Check | Result | Evidence |
|---|---|---|
| LICENSE is unmodified Apache-2.0 | PASS | byte-identical to <https://www.apache.org/licenses/LICENSE-2.0.txt> (SHA-256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`) |
| `pyproject.toml` declares `Apache-2.0` | PASS | `license = "Apache-2.0"` |
| NOTICE file | ADDED | did not exist; Apache-2.0 §4(d) relies on it for attribution |
| Copyright / SPDX notices in source files | ADDED | none of the 91 source files had one; every `.py`, `.sh`, `.js`, `.css`, `.html` now starts with `SPDX-FileCopyrightText` / `SPDX-License-Identifier: Apache-2.0` |
| Credentials in the tree or git history | none found | `repo_checks.py --history`: every blob of all 93 commits scanned for API keys, tokens and private keys. Matches were readable test placeholders only. |
| Sensitive files ever committed (`.env`, keys, databases, user data) | none found | history scan of all paths |
| Internal infrastructure details | FOUND, removed from the tree | a Tailscale address and a host name of the private Platform deployment were in the shipped contract provenance file, `docs/VERIFICATION.md` and two tests. **They remain in git history** (see OWNER DECISION 3). |
| Proprietary NATURE AI Platform server code | none found | no Platform package, service or database code is present (an earlier import scan is recorded in VERIFICATION.md, item I). The only Platform-derived file is the Client Interface contract (OWNER DECISION 2). |
| Third-party licenses | PASS, with notes | [THIRD_PARTY.md](../THIRD_PARTY.md): all runtime dependencies are permissive, except certifi (MPL-2.0, used unmodified). Notes cover release bundles, Windows-only colorama and Docker base images. |
| Release artifacts committed | none | checked in CI from now on |

## 4. OWNER DECISIONS (not decided in this repository)

1. **Copyright holder name.** NOTICE and the SPDX headers say "The Nature AI Worker Authors", meaning the contributors recorded in git history. That claims no particular legal entity. If a company holds or will hold the copyright (for example by employment or assignment), replace the phrase in `NOTICE` and the headers. A single search-and-replace is enough; `repo_checks.py` checks only the `SPDX-License-Identifier` line.
2. **Publishing the Platform Client Interface contract.** `src/nature_worker/contracts/client-interface-v1.openapi.json` is extracted from the Platform's OpenAPI document (28 Client Interface operations and their schemas). Committing it publishes it under Apache-2.0. It is an interface description, not server code, and the S–C client needs it. Confirm that publishing it is intended, or ship it separately under other terms. It also describes three operations the Worker does not call (OIDC callback, OIDC login, recording authorization).
3. **Internal addresses in git history.** Removing the private deployment's Tailscale address and host name from history requires rewriting history (for example with `git filter-repo`) and force-pushing, which changes every later commit hash for everyone. A tailnet address is not reachable from the internet, so the exposure is limited. Decide whether to rewrite before the repository is made more widely public.
4. **Trademark registration.** No registration is claimed. Whether to register "NATURE AI" or the logo, and in which countries, is a legal and business decision.
5. **Contributor policy.** CONTRIBUTING.md now asks for DCO sign-off. CI does **not** enforce it yet: earlier commits on the branch have no sign-offs, so enforcing it now would fail every open PR. Decide whether to enforce sign-off for new commits (for example with a DCO GitHub App), or to use a CLA instead.
6. **Official release key.** See [RELEASES.md](RELEASES.md#setting-up-the-official-key).

## 5. Repository checks

`python scripts/repo_checks.py` runs in CI on every push and PR:

- the license text, NOTICE and the declared license;
- SPDX headers;
- credentials and private keys, sensitive file names, internal addresses and tailnet host names outside tests, and confidentiality markers;
- release artifacts, binaries and files over 2 MB;
- with `--licenses`: every locked dependency's license, and that it is listed in THIRD_PARTY.md;
- with `--history`: every blob in history (needs a full clone; run it before publishing the repository).

These are pattern checks: they catch common accidents and cannot prove that something is absent.
