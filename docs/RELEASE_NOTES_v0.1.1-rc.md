# Nature AI Worker 0.1.1 — Windows Standalone Internal RC

**Status:** Internal testing only. Not an official tagged or published release.

## Changes since the Windows 0.1.0 RC

- New Work and work conversations include a `+` attachment picker, uploading through the existing Standalone workspace file API and adding a file reference to the task instructions.
- The Windows Standalone desktop launcher uses a version-local `NatureAIWorker.exe` interpreter copy and identifies its console window as `Nature AI Worker (Standalone)`. Task Manager Details visibility must be confirmed on the Testing Computer.
- Existing Worker task execution, approvals, data retention, and installation isolation contracts remain in force.

## Required Windows testing

Upgrade the existing Windows 0.1.0 Standalone installation to 0.1.1 without wiping user data or workspace files. Verify task history, provider configuration, the existing `windows_rc_test.txt`, new `+` attachment behavior, Task Manager Details process identity, shutdown/relaunch, and uninstall behavior.

This internal test build includes a verified Ed25519 release manifest. Official publication requires a version-matching tag, provenance verification, publication checks, and outstanding licensing decisions. Never move the existing `v0.1.0` tag.
