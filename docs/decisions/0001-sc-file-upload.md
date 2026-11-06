# Decision 0001: No file upload in the Platform Client (Client Interface v1.3)

Status: **open product decision**. Recorded 2026-10-26.

## Context

The Platform's Client Interface v1.3 (28 operations, `src/nature_worker/contracts/client-interface-v1.openapi.json`) lets a client list a Task's files (`listTaskFiles`) and download one (`downloadTaskFile`). It has **no operation that accepts a file from the client**. Task files are deliverables an Agent saved on the Platform.

The Standalone Worker has a workspace upload (`PUT /api/files`). That is local to Standalone and must not be reused for S–C.

## Decision for now

The Platform Client implements download only. It offers no upload control and does not send file content through chat messages, template fields or any other operation as a workaround. A contract test (`test_contract_has_no_upload_operation`) fails if an upload operation appears, so adding one is a deliberate change to this decision.

## Questions for the product owner

1. Should users attach input files to a Task at all, or only to particular templates?
2. Where are uploaded files governed? This covers retention, malware scanning, size and type limits, tenant isolation, and whether a file counts as a Task input or a Task deliverable.
3. How does approval apply? For example, may an Agent read an uploaded file only after approval?
4. What should the operation look like (for example a multipart `POST .../tasks/by-number/{n}/files`), and in which Client Interface version?

## Consequences

Users who need an Agent to work from a document must make it available through the Platform's own channels until the Platform adds an upload operation and the Worker implements it against the published contract.
