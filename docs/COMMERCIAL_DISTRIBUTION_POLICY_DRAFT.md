# Nature AI Worker — Commercial Product Redistribution Policy (Decision Draft)

> **OWNER INTENT, NOT YET IMPLEMENTED (2026-11-05).** Nature AI Corp wants individuals and companies to be able to use Nature AI Worker directly without a compulsory fee, **but wants companies that embed, repackage, bundle or redistribute Nature AI Worker inside their own products to pay NATURE AI CORP for a commercial distribution/OEM license.** This is a revenue model, not a request to pay vendors for distribution.

## Existing licensing conflict

Current repository source, metadata and distributed artifacts declare **Apache License 2.0**. Apache-2.0 allows anyone to use, modify and **redistribute Nature AI Worker as part of a commercial product without a mandatory royalty**, subject to attribution and other license conditions. NATURE AI CORP cannot use additional policy text to impose a mandatory OEM fee on already licensed Apache-2.0 copies, and future relicensing (if permitted by rights ownership) would not revoke rights already granted. The [Open Source Definition](https://opensource.org/osd) also prohibits mandatory royalties for redistributed components (§1).

Therefore the exact desired restriction and **OSI-approved open source** cannot both apply to the same released software under a single license.

## Options for an explicit owner decision

| Policy model | Personal/direct business use | Third-party incorporation/redistribution in commercial products | Open-source status |
| --- | --- | --- | --- |
| **A — retain Apache-2.0** | Free | Free as a license right; optionally sell separate OEM services, warranties, official branding, proprietary components, or distribution support | OSI open source |
| **B — prospective free-use + commercial redistribution license** | Free under explicit limited source-available terms | Requires commercial/OEM agreement and license fees | **Not OSI open source** for the restricted code |
| **C — prospective dual offering** | Open edition under Apache-2.0 remains free | A distinct proprietary edition, functionality, official OEM package, or additional service offered under paid agreement; Apache edition still freely redistributable | Apache edition remains open source; OEM fee applies only to value not already licensed for free |

**Key distinction:** dual licensing the **same identical source** under Apache-2.0 and an optional paid commercial license cannot, by itself, force all commercial distributors to buy the paid license: they can choose Apache-2.0. A compulsory payment for embedding the same code requires prospective restrictions (option B), or valuable proprietary assets/contract benefits beyond Apache rights (option C).

## Before changing the license

1. Identify all copyright holders and contributor grants/assignments; confirm Nature AI Corp has the rights to apply the new terms to any future release.
2. Distinguish **ordinary use, hosting a private internal service, customization for own use, embedding into another downloadable product, physical OEM bundling, white-label distribution, SaaS offering, and trademark/official-brand usage**. Define which are free and which require payment.
3. Decide whether being an **OSI-approved open-source project** is more important than compulsory OEM fees on the identical code. Be accurate in public claims.
4. Have counsel review the prospective commercial/source-available agreement, existing Apache rights, third-party software licenses, enforceability, and customer notice/acceptance.
5. Only after owner approval, adjust LICENSE, source SPDX headers, NOTICE, pyproject metadata, README, trademarks guidance, release notes and installer legal terms consistently; test a new packaging build.
6. Do not change historical tags or claim that a new license retroactively restricts earlier Apache versions.

## Release gate

**Do not publish v0.1.1 or change repository visibility until the owner chooses the licensing approach.** The existing private release candidate can continue to be tested. Draft policy text is not a license and imposes no new conditions by itself.
