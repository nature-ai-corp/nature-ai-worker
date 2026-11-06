# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Public, read-only GitHub issue inspection for the NATURE AI Worker community.

This first slice intentionally has no authentication and no write capability.
The allowlisted repository and HTTPS API host are constants, not model input.
GitHub response bodies are untrusted data and must not become agent instructions.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx

from .workspace import ToolError

REPOSITORY = "nature-ai-corp/nature-ai-worker"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
MAX_ITEMS = 30
MAX_CHARS = 16000


def github_issues(_workspace, args: dict) -> str:
    """Read a bounded page of public issues or inspect one issue by number.

    Pull requests are excluded from the issue listing. No user credentials are
    accepted or needed. This tool cannot address another repository or endpoint.
    """
    mode = args.get("mode", "list")
    if mode not in ("list", "issue"):
        raise ToolError("mode must be 'list' or 'issue'.")
    number = args.get("number")
    if mode == "issue":
        if type(number) is not int or not (1 <= number <= 10_000_000):
            raise ToolError("issue mode requires a positive integer number.")
        path = f"/issues/{number}"
        params = None
    else:
        if number is not None:
            raise ToolError("number is supported only in issue mode.")
        state = args.get("state", "open")
        if state not in ("open", "closed", "all"):
            raise ToolError("state must be 'open', 'closed', or 'all'.")
        page = args.get("page", 1)
        if type(page) is not int or not 1 <= page <= 3:
            raise ToolError("page must be an integer from 1 to 3.")
        path = "/issues"
        params = {"state": state, "page": page, "per_page": MAX_ITEMS, "sort": "updated", "direction": "desc"}

    try:
        # A hardcoded HTTPS endpoint, redirects disabled, and no credential
        # forwarding keep repository access strictly within the public scope.
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False,
                          headers={"Accept": "application/vnd.github+json",
                                   "User-Agent": "NatureAIWorker-Community-ReadOnly"}) as client:
            response = client.get(API_ROOT + path, params=params)
        if response.status_code == 403 or response.status_code == 429:
            raise ToolError("GitHub API rate limit or access restriction. Try again later.")
        if response.status_code == 404:
            raise ToolError("GitHub issue not found.")
        if response.status_code != 200:
            raise ToolError(f"GitHub API request failed (HTTP {response.status_code}).")
        if len(response.content) > 1_000_000:
            raise ToolError("GitHub response exceeded the allowed size.")
        payload = response.json()
    except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
        raise ToolError("Unable to read public GitHub issues right now.") from exc

    if mode == "issue":
        if not isinstance(payload, dict) or "pull_request" in payload:
            raise ToolError("The requested item is not a public GitHub issue.")
        results = [_summarize(payload, detailed=True)]
    else:
        if not isinstance(payload, list):
            raise ToolError("Unexpected GitHub API response.")
        results = [_summarize(item, detailed=False) for item in payload
                   if isinstance(item, dict) and "pull_request" not in item]
    return json.dumps({"source": "public GitHub API (untrusted community content)",
                       "repository": REPOSITORY, "mode": mode, "issues": results},
                      ensure_ascii=False)[:MAX_CHARS]


def _summarize(item: dict, *, detailed: bool) -> dict:
    number = item.get("number")
    if type(number) is not int or not 1 <= number <= 10_000_000:
        raise ToolError("Unexpected issue number in GitHub response.")
    # Only canonical issue links from the allowlisted repository are returned.
    url = f"https://github.com/{REPOSITORY}/issues/{number}"
    result = {
        "number": number,
        "url": url,
        "title": _text(item.get("title"), 350),
        "state": _text(item.get("state"), 24),
        "updated_at": _text(item.get("updated_at"), 40),
        "labels": [_text(x.get("name"), 60) for x in item.get("labels", [])[:12]
                   if isinstance(x, dict)],
        "comments": item.get("comments") if type(item.get("comments")) is int else None,
    }
    if detailed:
        result["body"] = _text(item.get("body"), 9000)
    return result


def _text(value, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""
