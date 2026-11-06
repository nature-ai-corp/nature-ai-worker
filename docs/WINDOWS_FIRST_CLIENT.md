# Nature AI Worker — Windows-first client policy

## Product priority
Windows desktop is the primary business client for Nature AI Worker. The enterprise
Platform Client (S–C) is the primary business workflow. Standalone remains a
minimal general-purpose Worker, not a duplicate enterprise runtime.
Linux is supported secondarily and must not drive Windows architecture choices.

## Windows release gates (P0)
- Native Windows desktop packaging/installation, launch, upgrade and uninstall
- Real shared Platform S–C Interface integration and Platform authentication;
  no invented API contract or local enterprise Agent runtime
- Windows Credential Manager / DPAPI backed secrets and correct NTFS ACLs;
  no enterprise keys in a Standalone-accessible directory
- Enforced separation of Standalone and enterprise process/data permissions
- Transactional Standalone -> S–C: prepare/authenticate/validate before commit.
  On failure, revoke staged sessions, clear and verify client enterprise residue,
  preserve original Standalone data; block local Agent if cleanup is unverified
- No in-place S–C -> Standalone. Require authorized uninstall, enterprise cleanup,
  independent Clean Validation and new Standalone install
- Windows integration tests (real Windows OS), interrupted-transition recovery,
  multi-user security checks, and end-to-end Platform workflows

## CI policy
Run Windows client security and integration tests on Windows runners as a
release blocker. Linux tests remain regression coverage, not a substitute for
Windows testing. Simulated positive check callbacks and Linux tests alone do
not establish Windows release readiness.

## Scope discipline
Prefer Platform's existing S–C Interface. Do not build a separate enterprise
business API or local Agent/Task/approval engine. Maintain open-source client
components and leave proprietary Platform services on the server.
