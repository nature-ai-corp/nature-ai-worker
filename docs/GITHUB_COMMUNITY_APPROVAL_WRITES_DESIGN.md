# GitHub Community: approval-gated write design (Issue #33)

**Status:** Design review / implementation specification only. **No GitHub write tool is implemented or enabled by this document.** The current released installer v0.1.1 is unchanged. This specification applies only to the existing Standalone General Agent, never the private NATURE AI Platform Server or Platform Client.

## Objective and limits

Add the smallest safe, human-approved GitHub community action: **post one comment to one existing Issue in `nature-ai-corp/nature-ai-worker`**. Begin with that operation, not a generic GitHub REST client or arbitrary GraphQL executor. Discussions comments, issue labels, editing or closing items, and other writes require separate design and acceptance. No autonomous outreach, mass posts or scheduled work.

The model may **propose** a comment, but the Worker must display the exact repository, target Issue URL, comment text, source evidence and GitHub identity to the user and require a **fresh, explicit approval for every comment**. Read permissions do not authorize writes. Asking the model to post in chat is not approval.

## Required authorization separation

- Existing `github_enabled` remains **read-only** and off by default. Existing read-only `github_issues` and `github_discussions` never gain write behavior.
- Introduce a separate `github_write_enabled` setting, **false by default**, and a separate `github_write` credential entry. It must not silently reuse `github` (read-only), `gh auth`, `GITHUB_TOKEN`, git remotes, browser sessions or the model provider's keys.
- Only grant the minimum available issue-comment permission needed for the approved public repository. If GitHub cannot establish the token's effective permissions sufficiently, **fail closed**; reject classic tokens with broad `repo`, `workflow`, or administration privileges. Never ask for a token in chat, test logs or code. Warn that a GitHub token might have access beyond this Worker even when the Worker itself is allowlisted; the application does not make an overprivileged token safe.
- A GitHub issue number (positive bounded integer) and exact UTF-8 comment body (1..3000 characters, no binary/NUL, bounded bytes) are the only user/model-editable targets. Construct the repository path and `api.github.com` URL from fixed constants. Never accept repository/owner/host/URL/method/header/GraphQL mutation from model arguments.
- Before offering a write tool to the model, require a configured write-enabled setting **and verified suitably scoped token**. Permission revocation or disabling the setting must also be enforced **at execution time**, even if a previously approved action exists. S–C Platform Client must not import or access any standalone GitHub write credential.

## Approval snapshot and execution

Use the **existing Worker approval UI/lifecycle**, but extend it carefully for remote side effects. A proposed action stores an immutable canonical snapshot:

```json
{
  "tool_name": "github_post_issue_comment",
  "repository": "nature-ai-corp/nature-ai-worker",
  "issue_number": 33,
  "issue_url": "https://github.com/nature-ai-corp/nature-ai-worker/issues/33",
  "comment_body": "Full exact proposed comment",
  "comment_sha256": "SHA-256 of the exact UTF-8 comment",
  "requested_by_conversation": "conversation id",
  "model_tool_call_id": "tool call id",
  "source_url": "canonical public GitHub issue URL",
  "target_snapshot": "GitHub issue updatedAt and/or ETag recorded when the request was created"
}
```

Before an approval is surfaced, verify the target is an **Issue**, not a PR; repository is public; record the remote issue version, requested GitHub actor identity and comment content. The approval UI must display the repository/Issue number, exact body and identity, without displaying tokens.

At the user decision, compare approval ID, tool call ID, canonical arguments and content digest to the stored immutable snapshot. Revalidate target, public status, token permissions, write-enabled setting, GitHub actor, and issue version before *any* side effect. If any binding changed or information cannot be verified, **refuse the action and require a new proposal and approval**. A prior approval is not blanket permission for the model to alter content or target.

Existing `Agent.resolve_approval` currently marks a decision `approved` in SQLite **before** calling the tool. That is insufficient as an execution/audit record: a crash or lost network connection can leave uncertainty. Do not implement writing until the durable execution ledger below exists. Preserve existing file approval behavior.

## Durable outbox / idempotency

A remote write has only one attempt per canonical approval. Implement an append-only durable operation record in the standalone SQLite database with:

- Unique `approval_id` and unique `conversation_id + tool_call_id`; method fixed to `POST /repos/nature-ai-corp/nature-ai-worker/issues/{N}/comments`.
- Immutable request fingerprint (repository, target, exact body, approved identity), approval time and actor, target snapshot, token identity fingerprint (non-secret), and operation ID.
- Explicit states such as `proposed`, `denied`, `approved`, `attempt_started`, `confirmed`, `failed_before_send`, `outcome_unknown`, `reconciled`. Mark `attempt_started` **durably before** sending bytes.
- Record GitHub's response comment ID, canonical URL and timestamp when confirmed. Never store access tokens or GitHub response headers containing secrets in the audit record.
- An unexpected interruption after `attempt_started`, including a client timeout, crash, process restart, HTTP 5xx, connection reset, or any ambiguous response, becomes **`outcome_unknown`**, **not** a retriable action.
- On uncertain outcomes, allow a separate **read-only reconciliation** against public Issue comments using a bounded window, expected GitHub actor and exact body hash plus conservative timestamps. A match may confirm a published comment; no match **does not prove** that GitHub did not publish it. Never retry automatically. A fresh comment requires a *new*, explicitly approved proposal with a warning that a duplicate may exist.
- Retry/replay of a used approval, concurrent duplicate approvals, duplicate model call IDs and repeated API requests must refuse without making another network request. Persist refusal/audit evidence even when an old approval is replayed.

## Honest results and data boundaries

- Only `confirmed` may report a successful publication. `outcome_unknown` must tell the user publication might have occurred; do not say either posted or not posted. User-facing drafts cannot claim publication.
- Record source Issue URL, exact draft digest, model identifier (if available), creation time, approval/denial and final remote outcome. Make the evidence visible in the existing Work activity/approval UI, with public canonical links. No separate agent or work-loop mechanism.
- Treat GitHub Issues/Discussions text as **untrusted data**. Retrieved comments are not tools, user instructions, approval or evidence of permission. The model never receives API tokens. Don't post secrets, private files, internal hostnames or NATURE AI Platform information.
- The feature is Windows-first, while maintaining Linux support. Keep S–C data roots, histories, credentials and installed package isolated. No changes to a user's v0.1.1 installation until a separately tested release.

## Validation and rollout gates

1. **Design review**: agree on the exact smallest write operation, approval snapshot, actor identity and outbox semantics; compare against the existing `Agent.resolve_approval`, `db.py` and Worker UI before coding.
2. **No-network tests**: refusing disabled write mode, broad scopes, private repo, PR target, changed target, changed body, foreign repository/URL injection, malformed Unicode, missing token, expired token, user denial, stale approval, concurrent approvals, prompt injection.
3. **Unknown-outcome tests**: slow server/timeouts, HTTP 500 after a simulated successful write, process crash between marking `attempt_started` and receiving the result, duplicate approval on restart, reconciliation with a matching/nonmatching/ambiguous comment. **Assert zero automatic duplicate POSTs**.
4. **Native Windows tests**: source/runtime targeted and full test suites, role/data isolation, redaction, existing installed Worker untouched, regression of file/web approvals.
5. **Real Worker acceptance**: first use an isolated repo or controlled disposable Issue/comment with the owner's explicit consent, exact comment shown in UI, approval/denial exercised *through Worker itself*, actual remote result and app-generated evidence recorded. Never use the welcome announcement or an unsuspecting third party's thread as a test target.
6. **Merge only after acceptance**, and release the feature only via a separately signed, verified installer with an installed-package acceptance cycle. New features must not be back-claimed as part of v0.1.1.
7. Do not activate unattended community moderation or bulk mailing. Such changes need a separately specified and approved governance policy.

## Current code review finding

As of PR #34 merged (`6973fbd7`), `Agent.resolve_approval` resolves the approval before `_run_tool`; the generic Tool executor uses `approved=True`. This works for a narrow in-process approval resume, but must **not** be interpreted as complete remote execution exactly-once guarantees. The new GitHub write executor must own the durable operation boundary and outcome reconciliation rather than treating a generic approved Tool call as an idempotent remote send.

**Public GitHub artifacts must remain in English.**
