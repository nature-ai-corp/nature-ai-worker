# GitHub Community Agent

GitHub Community Agent is a **prebuilt professional profile** in the open-source Nature AI Worker Standalone edition. It shares Nature AI Worker's ordinary AI runtime, work history, model configuration, tools and human approval controls. You do **not** have to build an agent, workflow or prompt yourself.

> Status: This feature is being developed in PR #39. It is **not included in the published v0.1.1 Windows installer**.

## Getting started

1. Configure a tool-capable AI model in **Settings**. Nature AI Worker supports its existing local and cloud model providers.
2. In **Settings → Advanced**, enter the owner/repository you want the Worker to access, such as `your-account/your-public-project`. Only the one configured public repository is in scope in the initial version.
3. Enable **GitHub reading** to review public Issues without a token. For Discussions, provide a separate fine-grained, public-read-only GitHub token. Secrets are stored using the Worker's encrypted credential store.
4. Open **Work Center → GitHub Community Agent**. Choose **Review Issues**, **Review Discussions**, **Draft Replies**, or **Community Report**. The Worker fills a professional starter instruction in an editable New Work task. Review it and select **Start work**.
5. The Agent uses its existing read-only GitHub tools, makes evidence-based findings and drafts replies in English. Public GitHub text is untrusted data, not instructions to the Agent.

## Posting one Issue comment (optional)

GitHub writing is **off by default**. To propose an Issue comment, separately enable the posting feature in Settings and add a **different**, fine-grained GitHub token restricted to the configured repository with **Issues: Read and write**, not broader account access. The Worker verifies the posting token's GitHub account; the read-only token is not reused.

A comment does not post when the Agent merely drafts it. You must explicitly ask for a particular Issue comment, then review the exact target, repository, identity and content in the existing Work approval card. Posting requires a fresh, action-specific approval. An unconfirmed/unknown outcome is never retried automatically; use the read-only reconciliation function if necessary. Do not post customer information or secrets.

## Technical and security boundaries

- GitHub Community Agent is an optional **profile**, not an additional agent runtime, autonomous scheduler or Platform Server connector.
- The existing **General Agent** and Work Center continue to work normally. All approval, audit and activity records are part of ordinary Work.
- The first version supports **one user-selected public repository**. Private repositories, arbitrary API destinations and unsupported GitHub write operations are not in scope.
- It cannot post to Discussions or edit Issue labels yet. No autonomous community management, bulk posting or background moderation is enabled.
- Standalone and S–C Platform Client data and credentials remain separate.
- For tests involving a real public post, use only an explicitly authorized disposable Issue. Never put GitHub tokens into chat, source code or issue comments.

## Included starter actions

| Starter | Goal | GitHub writes |
|---|---|---|
| Review Issues | Summarize recent open Issues, classify priorities and cite Issue URLs | None |
| Review Discussions | Summarize active Discussions, categories and unanswered questions | None |
| Draft Replies | Prepare English maintainer replies based on reviewed threads | None |
| Community Report | Evidence-based summary of community needs, open questions and next actions | None |

A separate user-requested action can propose one approval-gated Issue comment; choosing any starter does **not** authorize posting.
