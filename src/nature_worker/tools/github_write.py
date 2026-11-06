# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""The one approval-gated GitHub write: post one comment on one Issue of the configured repository.

* Off by default, with its own setting and its own credential (never the read-only token).
  Only fine-grained personal access tokens with an expiry date are accepted: a classic token
  cannot be limited to one repository, so it is refused, as are tokens that do not say who
  they belong to.
* The model supplies only an issue number and the comment text. The repository is the one
  allowlisted in Settings (the token is verified for exactly that repository); host, path
  and method are constants. Before the approval is shown, the target is checked (public
  repository, an Issue rather than a pull request, not locked) and an immutable snapshot is
  stored in the outbox (``github_outbox.py``) with the approval request.
* Nothing is sent from the generic tool path. After the user approves, ``execute`` rebinds
  the approval to the snapshot, revalidates setting, token, GitHub identity and the Issue's
  version, records ``attempt_started`` durably, then sends exactly one POST. Every
  ambiguous ending is ``outcome_unknown`` and is never retried; ``reconcile`` only reads.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import time
from typing import TYPE_CHECKING, Callable

import httpx

from . import github_community as gc
from .workspace import ToolError

if TYPE_CHECKING:
    from ..github_outbox import Outbox
    from . import ToolContext

TOOL = "github_post_issue_comment"
OPERATION = "issue_comment"
MAX_BODY_CHARS = 3000
MAX_BODY_BYTES = 12_000
WRITE_TOKEN_PATTERN = re.compile(r"github_pat_[A-Za-z0-9_]{22,250}")
# Text that must never be published: credentials and private keys.
SECRET_PATTERNS = [
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
# Invisible direction overrides can make the approved text read differently from what is posted.
HIDDEN = re.compile("[‪-‮⁦-⁩‎‏﻿]")
RECONCILE_BEFORE = 120          # seconds before the attempt (clock difference)
RECONCILE_AFTER = 30 * 60       # seconds after the attempt
RECONCILE_PAGES = 3

DENIED = "The user denied posting this comment. Nothing was sent to GitHub."


def valid_write_token(token: str) -> bool:
    return bool(WRITE_TOKEN_PATTERN.fullmatch(token))


def token_fingerprint(token: str) -> str:
    """A non-secret identifier of a token: the first 16 hex digits of its SHA-256."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- arguments ---------------------------------------------------------------------


def normalize_arguments(args) -> dict:
    """The only accepted arguments, normalized exactly as they are stored and approved."""
    if not isinstance(args, dict) or set(args) != {"issue_number", "body"}:
        raise ToolError("github_post_issue_comment takes exactly 'issue_number' and 'body'.")
    number = gc._number(args["issue_number"])
    if number is None:
        raise ToolError("issue_number must be a positive integer.")
    body = args["body"]
    if not isinstance(body, str):
        raise ToolError("body must be text.")
    body = body.replace("\r\n", "\n").strip("\n")
    try:
        size = len(body.encode("utf-8"))
    except UnicodeEncodeError:
        raise ToolError("The comment contains invalid Unicode.") from None
    if not body.strip() or len(body) > MAX_BODY_CHARS or size > MAX_BODY_BYTES:
        raise ToolError(f"The comment must have 1 to {MAX_BODY_CHARS} characters.")
    if any(ord(ch) < 32 and ch not in "\n\t" or ord(ch) == 127 for ch in body):
        raise ToolError("The comment contains control characters.")
    if HIDDEN.search(body):
        raise ToolError("The comment contains invisible direction-control characters.")
    return {"issue_number": number, "body": body}


def _secret_problem(body: str, ctx: "ToolContext") -> str | None:
    if any(p.search(body) for p in SECRET_PATTERNS):
        return "The comment looks like it contains a credential or private key. It was not proposed."
    if any(s and len(s) >= 8 and s in body for s in ctx.known_secrets()):
        return "The comment contains a stored credential. It was not proposed."
    root = str(ctx.workspace.root)
    if len(root) > 3 and root in body:
        return "The comment contains a local folder path of this computer. It was not proposed."
    return None


# -- GitHub reads (identity, repository, Issue) ---------------------------------------


def check_write_token(token: str, repo: str) -> str:
    """The GitHub login of a usable write token, or ToolError. Fails closed."""
    if not valid_write_token(token):
        raise ToolError("Only a fine-grained personal access token (github_pat_...) can be used to post.")
    status, headers, content = gc._send("GET", f"{gc.API_HOST}/user", token=token)
    if "x-oauth-scopes" in headers:
        raise ToolError("Classic tokens cannot be limited to one repository; use a fine-grained token.")
    gc._check_status(status, headers, "user")
    if not headers.get("github-authentication-token-expiration"):
        raise ToolError("The GitHub write token has no expiry date; create one that expires.")
    user = gc._json(content)
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
        raise ToolError("GitHub did not say who the write token belongs to.")
    status, headers, content = gc._send("GET", gc.api_root(repo), token=token)
    gc._check_status(status, headers, "repository")
    info = gc._json(content)
    if not isinstance(info, dict) or str(info.get("full_name", "")).lower() != repo.lower() \
            or info.get("private") is not False:
        raise ToolError(f"The write token does not see the public repository {repo}.")
    return login


def _target(token: str, repo: str, number: int) -> dict:
    status, headers, content = gc._send("GET", f"{gc.api_root(repo)}/issues/{number}", token=token)
    gc._check_status(status, headers, "issue")
    issue = gc._json(content)
    if not isinstance(issue, dict) or gc._number(issue.get("number")) != number:
        raise ToolError("Unexpected GitHub API response.")
    if "pull_request" in issue:
        raise ToolError(f"#{number} is a pull request; only Issues can be commented on.")
    if issue.get("locked") is True:
        raise ToolError(f"Issue #{number} is locked.")
    updated = issue.get("updated_at")
    if not isinstance(updated, str):
        raise ToolError("GitHub did not report the Issue's version.")
    return {"updated_at": updated, "state": gc._text(issue.get("state"), 24),
            "title": gc._text(issue.get("title"), 350)}


# -- proposal (the model's tool call) -------------------------------------------------


def propose(ctx: "ToolContext", args: dict, approved: bool):
    """Check everything that can be checked, then ask for approval with a snapshot."""
    from . import ApprovalRequired
    if approved:
        # Approved GitHub writes run only through ``execute`` and the outbox.
        raise ToolError("This GitHub write can only run through its approval record.")
    arguments = normalize_arguments(args)
    number, body = arguments["issue_number"], arguments["body"]
    problem = _secret_problem(body, ctx)
    if problem:
        raise ToolError(problem)
    token = ctx.github_write_token() if ctx.github_write_enabled else None
    if not token:
        raise ToolError("Posting GitHub comments is turned off in Settings.")
    repo = ctx.github_repository
    actor = check_write_token(token, repo)
    target = _target(token, repo, number)
    url = f"{gc.html_root(repo)}/issues/{number}"
    snapshot = {
        "tool_name": TOOL, "operation": OPERATION, "repository": repo,
        "method": "POST", "path": f"/repos/{repo}/issues/{number}/comments",
        "issue_number": number, "issue_url": url, "source_url": url,
        "issue_title": target["title"], "issue_state": target["state"],
        "target_updated_at": target["updated_at"],
        "comment_body": body, "comment_sha256": _digest(body),
        "actor": actor, "token_fingerprint": token_fingerprint(token),
        "arguments": arguments, "proposed_at": time.time(),
    }
    reason = f"Post a comment as @{actor} on {url}"
    raise ApprovalRequired(reason, github=snapshot)


# -- execution (after the user approved) ------------------------------------------------


def execute(outbox: "Outbox", op: dict, ctx: "ToolContext") -> str:
    """Revalidate, record the attempt, send once, record the outcome. Returns the tool result."""
    snap = op["snapshot"]
    try:
        _revalidate(op, ctx)
    except Exception as e:  # noqa: BLE001 - anything unverified fails closed, before sending
        reason = str(e) if isinstance(e, ToolError) else "the proposal could not be verified."
        detail = f"Not posted: {reason} Nothing was sent to GitHub. A new proposal and approval are needed."
        outbox.transition(op["id"], "approved", "failed_before_send", detail=detail)
        return detail
    token = ctx.github_write_token()
    if not outbox.transition(op["id"], "approved", "attempt_started", attempt_started_at=time.time(),
                             detail="Sending to GitHub."):
        return "Not posted: this approval was already used. Nothing was sent to GitHub."
    try:
        state, detail, fields = _send_once(token, snap)
    except BaseException:
        outbox.transition(op["id"], "attempt_started", "outcome_unknown",
                          detail="The attempt was interrupted; the comment may or may not have been published.")
        raise
    outbox.transition(op["id"], "attempt_started", state, detail=detail, **fields)
    return result_text(state, detail, fields.get("comment_url"))


def _revalidate(op: dict, ctx: "ToolContext") -> None:
    snap = op["snapshot"]
    if not ctx.github_write_enabled:
        raise ToolError("posting GitHub comments was turned off.")
    token = ctx.github_write_token()
    if not token:
        raise ToolError("there is no verified GitHub write token any more.")
    if token_fingerprint(token) != op["token_fingerprint"]:
        raise ToolError("the GitHub write token changed after the proposal.")
    if ctx.github_repository != op["repository"]:
        raise ToolError(f"{op['repository']} is no longer the allowlisted repository.")
    if normalize_arguments(snap["arguments"]) != snap["arguments"] or snap["arguments"]["body"] != op["body"]:
        raise ToolError("the stored proposal is inconsistent.")
    problem = _secret_problem(op["body"], ctx)
    if problem:
        raise ToolError(problem)
    if check_write_token(token, op["repository"]) != op["actor"]:
        raise ToolError("the GitHub write token now belongs to a different account.")
    target = _target(token, op["repository"], op["issue_number"])
    if target["updated_at"] != snap["target_updated_at"]:
        raise ToolError(f"Issue #{op['issue_number']} changed after the proposal.")


def _send_once(token: str, snap: dict) -> tuple[str, str, dict]:
    """One POST. Returns (state, detail, result fields). Never retries."""
    number = snap["issue_number"]
    url = f"{gc.api_root(snap['repository'])}/issues/{number}/comments"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": gc.USER_AGENT,
               "X-GitHub-Api-Version": "2022-11-28", "Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False,
                          transport=gc._transport) as client:
            with client.stream("POST", url, json={"body": snap["comment_body"]}, headers=headers) as response:
                content = b""
                for chunk in response.iter_bytes():
                    content += chunk
                    if len(content) > gc.MAX_BYTES:
                        break
                status = response.status_code
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # No connection was ever established, so no request bytes left this computer.
        return "failed_before_send", "Not posted: GitHub could not be reached. Nothing was sent.", {}
    except httpx.HTTPError as e:
        return "outcome_unknown", f"The connection failed during the attempt ({type(e).__name__}).", {}
    if status == 201:
        return _confirmed(content, snap)
    if 400 <= status < 500:
        return "rejected", f"GitHub refused the comment (HTTP {status}). It was not posted.", {}
    return "outcome_unknown", f"GitHub answered HTTP {status}; the comment may or may not have been published.", {}


def _confirmed(content: bytes, snap: dict) -> tuple[str, str, dict]:
    try:
        comment = json.loads(content)
    except ValueError:
        comment = None
    comment_id = comment.get("id") if isinstance(comment, dict) else None
    prefix = f"{snap['issue_url']}#issuecomment-"
    if (type(comment_id) is not int or comment.get("html_url") != f"{prefix}{comment_id}"
            or _digest(comment.get("body") or "") != snap["comment_sha256"]
            or (comment.get("user") or {}).get("login") != snap["actor"]):
        return "outcome_unknown", "GitHub accepted the request but its answer did not match the approved comment.", {}
    return "confirmed", "GitHub confirmed the comment.", {
        "comment_id": comment_id, "comment_url": f"{prefix}{comment_id}",
        "comment_created_at": gc._text(comment.get("created_at"), 40)}


def result_text(state: str, detail: str, url: str | None = None) -> str:
    if state in ("confirmed", "reconciled"):
        return f"Posted (confirmed by GitHub): {url}"
    if state == "outcome_unknown":
        return ("Outcome unknown: " + detail + " The comment MAY OR MAY NOT have been published. It will not be "
                "sent again. Tell the user exactly that; do not say it was posted or that it was not. The user can "
                "check it with Reconcile in the Work view.")
    return detail


# -- reconciliation (read-only) ---------------------------------------------------------


def reconcile(outbox: "Outbox", op: dict, token: Callable[[], str | None]) -> dict:
    """Look for the comment among the Issue's public comments. Never sends a write.

    Exactly one comment by the approved account, with the approved text, created in the
    attempt's time window, confirms it. No match does not prove it was not published."""
    if op["state"] != "outcome_unknown":
        outbox.record(op["id"], "reconcile_refused", f"state is {op['state']}")
        return {"state": op["state"], "result": "Only an operation with an unknown outcome can be checked."}
    started = op["attempt_started_at"]
    since = datetime.datetime.fromtimestamp(started - RECONCILE_BEFORE, datetime.timezone.utc)
    matches, seen = [], 0
    for page in range(1, RECONCILE_PAGES + 1):
        status, headers, content = gc._send(
            "GET", f"{gc.api_root(op['repository'])}/issues/{op['issue_number']}/comments",
            params={"since": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "per_page": 100, "page": page},
            token=token())
        gc._check_status(status, headers, "issue")
        items = gc._json(content)
        if not isinstance(items, list):
            raise ToolError("Unexpected GitHub API response.")
        seen += len(items)
        for c in items:
            if _matches(c, op, started):
                matches.append(c)
        if len(items) < 100:
            break
    if len(matches) == 1:
        c = matches[0]
        url = f"{op['snapshot']['issue_url']}#issuecomment-{c['id']}"
        outbox.transition(op["id"], "outcome_unknown", "reconciled",
                          detail="Found the approved comment on GitHub; it was published once.",
                          comment_id=c["id"], comment_url=url, comment_created_at=gc._text(c.get("created_at"), 40))
        return {"state": "reconciled", "result": f"Published: {url}"}
    event = "reconcile_ambiguous" if matches else "reconcile_no_match"
    detail = (f"{len(matches)} matching comments" if matches else
              f"no matching comment among {seen} checked; this does not prove it was not published")
    outbox.record(op["id"], event, detail)
    return {"state": "outcome_unknown", "result": detail[0].upper() + detail[1:] + "."}


def _matches(c, op: dict, started: float) -> bool:
    if not isinstance(c, dict) or type(c.get("id")) is not int:
        return False
    if (c.get("user") or {}).get("login") != op["actor"] or _digest(c.get("body") or "") != op["body_sha256"]:
        return False
    try:
        created = datetime.datetime.strptime(c.get("created_at") or "", "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    ts = created.replace(tzinfo=datetime.timezone.utc).timestamp()
    return started - RECONCILE_BEFORE <= ts <= started + RECONCILE_AFTER
