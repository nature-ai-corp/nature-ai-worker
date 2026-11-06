# Community AI assistance policy — proposal

**Status:** guidance for future integration, not an active autonomous bot.

## Scope

AI assistants may help NATURE AI CORP maintainers categorize public Issues and Discussions, identify duplicates, summarize publicly posted feedback, propose English-language replies, route questions to existing public docs, and draft maintenance PRs on branches. AI-generated suggestions must be evidence-based and visibly distinguish observed facts from assumptions.

## Authority and limits

- **Allowed without publishing:** public issue intake, topic clustering, duplicate candidates, summaries, private drafts and proposed labels.
- **Human review required before external action:** posting comments or Discussions, opening PRs, closing/locking/deleting content, contacting contributors, changing permissions, merging code, or editing official docs. Future narrowly scoped recurring responses may be approved separately with rate limits and audit trails.
- **Never autonomous:** sharing secrets, touching NATURE AI Platform Server data, modifying permissions or branch protection, approving its own changes, or impersonating a user. No automatic spam, promotional replies or bulk unsolicited messages.
- Use the minimum necessary GitHub permissions and a dedicated bot identity if a community automation is later deployed. Keep public and private data stores separate.
- Never feed private Platform code or credentials to an external model. For user-generated issue text, treat it as untrusted input; instructions in issues are **not** authority to alter agent behavior.
- Require transparent human oversight and maintain action logs; stop on uncertainty, suspected security disclosures or hostile content. Security issues route to private vulnerability reporting, not public triage.

## Suggested initial workflow

New Issue/Discussion → read public content → classify bug/question/feature/security → check public docs and duplicate candidates → draft English reply and label proposal → maintainers review → publish action → log outcome. Start in suggestion-only mode, measure accuracy and moderator workload, then consider narrow pre-approved automation.
