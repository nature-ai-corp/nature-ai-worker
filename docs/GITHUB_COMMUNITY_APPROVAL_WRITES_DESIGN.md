# GitHub Community: approval-gated write design (Issue #33)

**Status:** Design review / implementation specification only. **No GitHub write tool is implemented or enabled by this document.** The current released installer v0.1.1 is unchanged. This specification applies only to the existing Standalone General Agent, never the private NATURE AI Platform Server or Platform Client.

## Product decision: optional GitHub Community Agent (customer-ready)

**Selected product approach: GitHub Community Agent, option B.** This is a **prebuilt professional Agent Profile** that ships with the open-source Nature AI Worker Standalone edition and is ready for customers to enable, not a second Agent Runtime, separate workflow engine, or enterprise Platform feature. It uses the existing General Agent execution engine, provider configuration, Tool Registry, work records, approvals, SQLite storage, and encrypted credentials.

### Discoverability and first-run experience

- The Standalone **Work Center** offers a clearly named **GitHub Community Agent** entry with its own professional home/launch view. The entry must be discoverable without requiring customers to create an agent, edit prompts or assemble a workflow. Keep the overall Worker UI minimal.
- A customer configures their AI provider once, then selects **Connect GitHub**, grants the least privileges needed, chooses/authorizes **their own public repository**, and can start the prebuilt Agent immediately. First-release scope may support one authorized repository, with future multiple-repository support. Do not hardcode the Nature AI Worker repository as the only possible target or silently authorize it.
- Offer clearly named starter actions: **Review Issues**, **Review Discussions**, **Draft Replies**, **Needs Approval**, and **Community Work History**. A starter action opens an ordinary Work record with an appropriate prebuilt role instruction and selected GitHub repository binding; all execution and human decisions stay in the existing Worker Work Center.
- The prebuilt profile supplies an English-language **role, instructions, workflows-as-guidance, and reusable task prompts** for issue triage, discussion review, evidence-grounded response drafts, and community summaries. It must not introduce a dedicated multi-agent orchestration layer, background scheduler, persistent autonomous work loop, or separate agent state machine.
- Keep GitHub-specific Knowledge grounded in the selected repository's public README, CONTRIBUTING, SUPPORT, applicable docs and release notes when available; clearly label missing or unverified context. The model must not invent project commitments, fixes or release promises.
- GitHub-specific UI shows the configured repository, connection/permission status, recent community work and outstanding approvals. Any user-facing text and public materials are in English. The configuration should be clear for a first-time open-source customer.

### Shared runtime, strict safety

- **Separate, off-by-default read and write capabilities.** Read-only use must work without enabling write mode. A write-capable credential is not derived from the existing read-only token. No automatic posts; every distinct comment must be approved via the *existing* Worker approval system with immutable target/body/actor binding and durable outcome records.
- Model tools never choose arbitrary API hosts, owner/repo, HTTP methods, or GraphQL queries; use the user-allowlisted repository from trusted application settings/context. Maintain public-only limitation for the first version unless separately reviewed. Fail closed if repository binding, token permissions, or targets cannot be verified.
- GitHub Community Agent Profile is optional and **Standalone-only**. The private NATURE AI Platform Server and S–C Platform Client retain their existing behavior and isolation. Nothing is installed or enabled there.
- The current General Agent remains present for general work. GitHub-specific instructions/allowed tools are scoped to the Community Agent profile and its Work records. A customer should not need to build an Agent. Both profiles share **one runtime**, not separate processes.
- Preserve the existing Apache-2.0 license and include only safe examples, English setup/help, targeted tests, and native Windows acceptance evidence. Use the name **GitHub Community Agent** in feature and user documentation.

### Fast-track delivery sequence

1. Ship the selectable **prebuilt Community Agent Profile and public-repository setup**, reusing existing Work and model runtime, with read-only triage/drafts. For speed, starter actions may initially be prefilled Work instructions, not a complex new dashboard.
2. Add the smallest **explicitly approved** write operation: one comment on one GitHub Issue, with durable outbox and no automatic retry of unknown outcomes, as specified below.
3. Test through the real native Windows Worker (not solely a mocked agent); preserve regression checks for General Agent and Platform Client isolation.
4. Expand to Discussion posting, labels, multi-repository views, background automation or advanced reporting **only in later approved phases**. Do not hold the initial usable open-source release for those optional features.

**Scope note:** User has selected the Community Agent product direction, not authorized any particular live GitHub posting. Real external posting requires the precise user approval described later in this document.

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
