#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Real-runtime acceptance of the approval-gated GitHub Issue comment (Issue #33).

Drives an already running Worker through its HTTP API, as the web UI does, with the real
model and the real GitHub API. It posts AT MOST ONE public comment, on an Issue the owner
explicitly authorized as disposable, and only with --i-authorize-one-public-comment.
Configure the Worker first in its Settings (model; allowlisted repository; posting enabled
with a fine-grained write token). Tokens are never accepted, printed or written here.

    python scripts/github_write_acceptance.py --url http://127.0.0.1:18034 --issue 36 \\
        --data-dir <data dir> --out github-write-acceptance.md --i-authorize-one-public-comment

Three proposals are made before any decision: P1 is approved (the one comment), its
approval is then replayed, P2 is approved after P1 changed the Issue (must be refused
without sending), P3 is denied. GitHub is then read to check exactly one comment exists.
Exit codes: 0 all passed, 1 a check failed, 2 not executed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


class Run:
    def __init__(self, url: str, out: Path):
        self.url, self.out = url.rstrip("/"), out
        self.rows: list[tuple[str, str, str]] = []
        self.notes: list[str] = []

    def api(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def record(self, name: str, ok: bool | None, detail: str) -> None:
        result = "PASS" if ok else "NOT EXECUTED" if ok is None else "FAIL"
        self.rows.append((name, result, detail))
        print(f"[{result}] {name}: {detail}", flush=True)
        self.write()

    def write(self) -> None:
        lines = ["# GitHub Issue comment acceptance (real Worker runtime)", "",
                 f"Date: {datetime.datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}", f"Worker: {self.url}", "",
                 "| Check | Result | Detail |", "|---|---|---|"]
        lines += [f"| {n} | {r} | {d.replace('|', '/')} |" for n, r, d in self.rows]
        if self.notes:
            lines += ["", "## Records", "", "```", *self.notes, "```"]
        self.out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def propose(self, issue: int, text: str) -> dict:
        _, conv = self.api("POST", "/api/conversations")
        prompt = (f"Post a comment on GitHub issue {issue} with exactly this text, nothing added or changed:\n\n"
                  f"{text}")
        _, sent = self.api("POST", f"/api/conversations/{conv['id']}/messages", {"text": prompt})
        _, c = self.api("GET", f"/api/conversations/{conv['id']}")
        return {"id": conv["id"], "status": sent.get("status"), "conversation": c}

    def conv(self, conv_id: str) -> dict:
        return self.api("GET", f"/api/conversations/{conv_id}")[1]


def github_comments(repo: str, issue: int) -> list[dict]:
    out = []
    for page in range(1, 6):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues/{issue}/comments?per_page=100&page={page}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "NatureAIWorker-Acceptance"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            items = json.loads(resp.read())
        out += items
        if len(items) < 100:
            return out
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True)
    p.add_argument("--issue", type=int, required=True, help="the disposable Issue the owner authorized")
    p.add_argument("--data-dir", type=Path)
    p.add_argument("--out", type=Path, default=Path("github-write-acceptance.md"))
    p.add_argument("--i-authorize-one-public-comment", action="store_true")
    args = p.parse_args()
    run = Run(args.url, args.out)
    if not args.i_authorize_one_public_comment:
        run.record("authorization", None, "--i-authorize-one-public-comment not given; nothing was posted")
        return 2
    _, s = run.api("GET", "/api/settings")
    repo = s.get("github_repository")
    ready = s.get("configured") and s.get("github_write_enabled") and s.get("github_write_token_source") == "stored"
    run.record("setup", bool(ready), f"provider={s.get('provider')} model={s.get('model')} repository={repo} "
                                     f"write_enabled={s.get('github_write_enabled')} "
                                     f"write_token={s.get('github_write_token_source')} actor={s.get('github_write_actor')}")
    if not ready:
        return 2
    marker = uuid.uuid4().hex[:8]
    texts = {k: f"Automated acceptance test of the Nature AI Worker approval-gated comment ({k}, run {marker}). "
                f"This disposable Issue was authorized for testing; please ignore."
             for k in ("P1", "P2", "P3")}
    before = github_comments(repo, args.issue)

    proposals = {k: run.propose(args.issue, t) for k, t in texts.items()}
    for k, prop in proposals.items():
        approvals = prop["conversation"]["approvals"]
        g = approvals[0]["github"] if approvals and approvals[0].get("github") else {}
        ok = prop["status"] == "needs_approval" and g.get("issue_number") == args.issue \
            and g.get("repository") == repo and g.get("actor") == s["github_write_actor"]
        run.record(f"{k}-proposal", ok, f"status={prop['status']} snapshot issue=#{g.get('issue_number')} "
                                        f"repository={g.get('repository')} actor=@{g.get('actor')} "
                                        f"text exactly as requested={g.get('comment_body') == texts[k]}")
        prop["approval"] = approvals[0] if approvals else None
    after_proposals = github_comments(repo, args.issue)
    run.record("no-post-before-approval", len(after_proposals) == len(before),
               f"comments before={len(before)} after three proposals={len(after_proposals)}")

    # P3: deny.
    status, _ = run.api("POST", f"/api/approvals/{proposals['P3']['approval']['id']}", {"approve": False})
    op3 = run.conv(proposals["P3"]["id"])["github_operations"][0]
    run.record("P3-deny", status == 200 and op3["state"] == "denied", f"HTTP {status} state={op3['state']}")

    # P1: approve - the one public comment.
    status, _ = run.api("POST", f"/api/approvals/{proposals['P1']['approval']['id']}", {"approve": True})
    c1 = run.conv(proposals["P1"]["id"])
    op1 = c1["github_operations"][0]
    answer1 = next((m["content"] for m in reversed(c1["messages"]) if m["role"] == "assistant" and m["content"]), "")
    run.record("P1-approve", status == 200 and op1["state"] == "confirmed" and bool(op1["comment_url"]),
               f"HTTP {status} state={op1['state']} comment={op1['comment_url']} "
               f"events={[e['event'] for e in op1['events']]}")
    run.notes.append(f"P1 agent reply: {answer1}")

    # Replay P1's approval.
    status, body = run.api("POST", f"/api/approvals/{proposals['P1']['approval']['id']}", {"approve": True})
    op1 = run.conv(proposals["P1"]["id"])["github_operations"][0]
    run.record("P1-replay", status == 409 and op1["events"][-1]["event"] == "refused_replay",
               f"HTTP {status} last event={op1['events'][-1]['event']}")

    # P2: approved after P1 changed the Issue - must be refused before sending.
    status, _ = run.api("POST", f"/api/approvals/{proposals['P2']['approval']['id']}", {"approve": True})
    c2 = run.conv(proposals["P2"]["id"])
    op2 = c2["github_operations"][0]
    answer2 = next((m["content"] for m in reversed(c2["messages"]) if m["role"] == "assistant" and m["content"]), "")
    run.record("P2-changed-target", op2["state"] == "failed_before_send" and "changed" in (op2["detail"] or ""),
               f"HTTP {status} state={op2['state']} detail={op2['detail']}")
    run.notes.append(f"P2 agent reply: {answer2}")

    time.sleep(3)
    after = github_comments(repo, args.issue)
    found = {k: [c for c in after if f"({k}, run {marker})" in (c.get("body") or "")] for k in texts}
    one = found["P1"][0] if len(found["P1"]) == 1 else {}
    ok = (len(found["P1"]) == 1 and not found["P2"] and not found["P3"]
          and one.get("body") == texts["P1"] and (one.get("user") or {}).get("login") == s["github_write_actor"]
          and one.get("html_url") == op1["comment_url"] and len(after) == len(before) + 1)
    run.record("github-state", ok, f"on GitHub: P1={len(found['P1'])} P2={len(found['P2'])} P3={len(found['P3'])} "
                                   f"comments {len(before)} -> {len(after)}; P1 body exact={one.get('body') == texts['P1']} "
                                   f"author=@{(one.get('user') or {}).get('login')} url matches={one.get('html_url') == op1['comment_url']}")
    for k in ("P1", "P2", "P3"):
        op = run.conv(proposals[k]["id"])["github_operations"][0]
        run.notes.append(f"{k} audit: " + " -> ".join(f"{e['event']}" for e in op["events"]))

    if args.data_dir:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from nature_worker.credentials import CredentialStore
        token = CredentialStore(args.data_dir).get("github_write", "https://api.github.com")[0]
        places = {p.name: p for p in args.data_dir.glob("worker.db*")}
        places.update({f"logs/{p.name}": p for p in (args.data_dir.parent / "logs").glob("*") if p.is_file()})
        leaks = [n for n, path in places.items() if token and token.encode() in path.read_bytes()]
        api_text = json.dumps(run.api("GET", "/api/settings")[1]) + "".join(
            json.dumps(run.conv(prop["id"])) for prop in proposals.values())
        if token and token in api_text:
            leaks.append("HTTP API")
        run.record("token-leak", bool(token) and not leaks, f"searched {sorted(places)} and the API: found in {leaks or 'none'}")
        del token
    run.write()
    print(f"Report: {args.out}")
    return 0 if all(r == "PASS" for _, r, _ in run.rows) else 1


if __name__ == "__main__":
    sys.exit(main())
