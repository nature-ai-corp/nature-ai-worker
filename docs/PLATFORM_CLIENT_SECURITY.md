# Platform Client security and mode-transition contract

Status: **approved architecture; implementation in progress**. This document specifies requirements, not evidence that controls have been implemented.

## Purpose and product boundary

Nature AI Worker remains open source. Standalone is a minimal, general-purpose introduction to the Worker interface; the enterprise Platform Client (S–C) is the strategic product. The Platform remains the source of truth for identity, task templates and lifecycle, Agent execution, authorization, approvals, files and governance. Do not embed the Platform runtime or introduce a Worker-specific enterprise backend.

## Modes

- `standalone`: local Agent, local model/provider keys, local workspace and history.
- `platform_client`: thin client of the Platform S–C Interface; no local Agent execution, provider API keys, or Standalone tools exposed.
- A fresh installation selects a mode through an explicit setup action. A valid transition from Standalone to Platform Client is supported.
- A Platform Client installation **must never** expose an in-place transition back to Standalone, even when disconnected or authentication expires.
- Returning to Standalone requires **Uninstall → explicit authorized S–C local-data cleanup → independent Clean Validation → fresh Standalone Install**. No reconfiguration shortcut, preservation of old local S–C credentials, or fallback mode.
- Client-side state is not a security authority: modified open-source clients can bypass UI rules. Platform server-side authorization and, for managed devices, OS/device management enforce enterprise controls.

## Storage, execution and credential isolation

1. Standalone and Platform Client must use independent data roots, databases, workspaces, caches, search indexes, logs, temporary files and credential namespaces. Never use a shared directory in which the Standalone Agent can search enterprise data.
2. The S–C runtime/API must have a separate execution and privilege boundary from the Standalone Agent runtime. Merely separating paths in the same OS user's permissions is not sufficient against a compromised local Agent.
3. Persist as little enterprise data as practical. Encrypt any S–C sensitive data at rest using OS-protected key material (Windows DPAPI or supported system credential service; OS equivalent for Linux). Do not save tokens in Standalone credentials or expose them to the model.
4. Session termination, account change, installation conversion and uninstall must zero/discard sensitive in-memory state and clear appropriate caches.
5. Log and crash-report policies must prevent S–C content, secrets and tokens from reaching Standalone history, logs, telemetry, or diagnostics.
6. S–C server data is not deleted by the Worker uninstaller. Enterprise export and local retention remain governed by the Platform.
7. Do not claim that Docker alone, chmod alone, or client encryption with a key available to the same compromised OS account constitutes strong process isolation. Validate the applicable OS boundary.

## Standalone Task Templates

Standalone has three **requirement-detail levels**, not authorization or capability levels:
- General: 5 requirement fields.
- Level 1: 10 fields (the same five plus five).
- Level 2: 15 fields (the same ten plus five).

Increasing detail does not change the user's original task nature or type of deliverable. All three generate the same ordinary Standalone work request and use the same local execution and approval policy. Fields must be editable, and the user should not have to invent facts to fill them.

## S–C Task Templates

Platform Client must fetch enabled Task Templates and Chat Templates, their classifications, presentation definitions and semantics from the connected Platform using the **existing shared S–C Client Interface**. It must not display or impose the Standalone General/Level 1/Level 2 levels, hard-code Platform template categories, or force Chat Template submissions to create a Task. Existing Platform authorization and Task governance are authoritative.

## Transition protocol

### Standalone → Platform Client
1. Preflight Platform compatibility and authentication.
2. Stop the Standalone Agent and prevent additional local work.
3. Do not migrate Standalone files, history or provider keys into the enterprise environment.
4. Initialize isolated, protected enterprise storage and establish Platform session.
5. Commit an atomic mode transition only after successful preflight and setup; interrupted transitions fail closed and are recoverable without exposing enterprise data to the local Agent.
6. Once S–C is activated, forbid Standalone launch until the mandatory uninstall-and-reinstall cycle.

### Platform Client → fresh Standalone
1. Initiate a dedicated uninstaller (not a mode toggle), stop S–C processes and invalidate local sessions.
2. Identify S–C owned storage and credentials; show a scoped manifest and obtain explicit authorization for local deletion, following enterprise retention and device policy. Do not delete unrelated user files.
3. Destroy encryption key material where allowed and remove all locally owned S–C data, credentials, indices, logs and markers requiring cleanup. Record failures without falsely reporting completion.
4. Independently validate the clean baseline, including stale running processes, OS credentials, app data and caches. If any relevant remnants or unverifiable conditions exist, **block** Standalone installation with actionable information.
5. Allow a separate, fresh Standalone installation only after Clean Validation succeeds. Never reuse S–C configuration or history.

For managed enterprise endpoints, stronger OS-level installer enforcement or device-management policy may be required: open-source applications running under an end user's control cannot guarantee tamper-proof cleanup.

## Required acceptance tests

- Verify local Agent tools and model context cannot access S–C workspace, cache, database, indices, tokens or logs.
- Verify S–C actions remain subject to Platform-side authorization, approvals and Task semantics (including Chat vs Task Template behavior).
- Simulate interruption during Standalone → S–C and verify fail-closed recovery.
- Assert all in-place S–C → Standalone APIs, UI controls and config edits fail.
- Test uninstall/cleanup manifest, consent, enterprise retention constraints, key destruction, and independently verified clean baseline.
- Inject residues individually (credential, cache, DB, temp file, search index, process, mode marker), confirm fresh Standalone installation is blocked.
- Test two Platform accounts/tenants do not see each other's local S–C state.
- Confirm no Standalone data silently migrates and no S–C data appears in diagnostics, backups or logs.
- Exercise Windows and Linux installation/uninstallation, plus documented privilege boundaries.

## Release gates

P0: working shared S–C integration, isolation enforcement, single-direction transition, scoped uninstall, independent clean pre-install check, Platform authorization verification, and real end-to-end tests. Do not label a GitHub CI unit-test pass as proof of enterprise isolation; require OS-level and genuine Platform runtime testing.
