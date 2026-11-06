#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Real-runtime acceptance of the GitHub Community Agent profile, through the real web UI.

Drives an already running Worker in a real Chromium browser (Chrome or Edge) exactly as a
user does: Work Center -> GitHub Community Agent -> a starter -> Start work. The model and
GitHub are real; GitHub posting must be OFF (the run refuses to start otherwise), so nothing
is ever published. Configure the Worker first in its Settings (model; repository; GitHub
reading; optionally a read-only token for Discussions). Keys are never read or printed,
except that --data-dir lets the run search the Worker's files for its own stored secrets
in-process and report only "found" or "not found".

    python scripts/github_community_agent_acceptance.py --url http://127.0.0.1:18036 \\
        --browser "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" \\
        --data-dir <data dir> --out community-agent-acceptance.md

Exit codes: 0 all passed, 1 a check failed, 2 not executed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))
from test_usability import Browser  # noqa: E402 - the repository's own minimal CDP client

POSTED = re.compile(r"\bI(?:'ve| have)? (?:posted|commented|published|replied on GitHub)\b", re.I)
# Offering to post, or asking for approval to post, while posting is turned off.
OFFERS = re.compile(r"\b(?:me|I) (?:to |can |could |will )?(?:post|publish|proceed with posting)\b|"
                    r"\bapprove\b[^.\n]*\bpost", re.I)
EXPLAINS = re.compile(r"\b(?:cannot|can't|can not|unable|not able|turned off|disabled|not enabled|Settings)\b", re.I)


class Run:
    def __init__(self, url: str, out: Path):
        self.url, self.out = url.rstrip("/"), out
        self.rows: list[tuple[str, str, str]] = []
        self.transcripts: dict[str, str] = {}

    def api(self, path: str):
        return httpx.get(self.url + path, timeout=60).json()

    def record(self, name: str, ok: bool | None, detail: str) -> None:
        result = "PASS" if ok else "NOT EXECUTED" if ok is None else "FAIL"
        self.rows.append((name, result, detail))
        print(f"[{result}] {name}: {detail}", flush=True)
        self.write()

    def write(self) -> None:
        lines = ["# GitHub Community Agent acceptance (real Worker runtime, real UI)", "",
                 f"Date: {datetime.datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}", f"Worker: {self.url}", "",
                 "| Check | Result | Detail |", "|---|---|---|"]
        lines += [f"| {n} | {r} | {d.replace('|', '/')} |" for n, r, d in self.rows]
        for name, text in self.transcripts.items():
            lines += ["", f"## {name}", "", "```", text, "```"]
        self.out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_work(run: Run, b: Browser, name: str, prepare) -> dict:
    """Prepare New Work in the UI, click Start work, wait for the run to end; return the record."""
    prepare()
    b.wait("!document.querySelector('#view-new').hidden")
    text = b.eval("document.querySelector('#new-input').value")
    start = time.monotonic()
    b.click("#new-start")
    b.wait("location.hash.startsWith('#/work/')", timeout=30)
    conv_id = b.eval("location.hash").split("/")[-1]
    b.wait("document.querySelector('#work-status') && /Finished|Failed|Stopped|Needs your approval|Interrupted/"
           ".test(document.querySelector('#work-status').textContent)", timeout=600)
    conv = run.api(f"/api/conversations/{conv_id}")
    messages = conv["messages"]
    calls = [(c["name"], c["arguments"]) for m in messages if m["role"] == "assistant"
             for c in (m.get("tool_calls") or [])]
    results = "\n".join(m["content"] for m in messages if m["role"] == "tool")
    answer = next((m["content"] for m in reversed(messages) if m["role"] == "assistant" and m["content"]), "")
    lines = [f"conversation: {conv_id}  title: {conv['title']}  status: {conv['work']['status']}  "
             f"seconds: {time.monotonic() - start:.0f}", f"task line: {text.splitlines()[0]}"]
    for n, a in calls:
        lines.append(f"tool call: {n} {json.dumps(a)}")
    lines.append(f"assistant: {answer}")
    run.transcripts[name] = "\n".join(lines)
    return {"id": conv_id, "conv": conv, "status": conv["work"]["status"], "calls": calls, "results": results,
            "answer": answer, "title": conv["title"], "text": text}


def traceable(answer: str, results: str, repo: str) -> tuple[bool, list[str]]:
    link = re.compile(rf"https://github\.com/{re.escape(repo)}/(?:issues|discussions)/\d+")
    cited = sorted(set(link.findall(answer)))
    return bool(cited) and all(c in results for c in cited), cited


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True)
    p.add_argument("--browser", required=True)
    p.add_argument("--data-dir", type=Path)
    p.add_argument("--out", type=Path, default=Path("community-agent-acceptance.md"))
    args = p.parse_args()
    run = Run(args.url, args.out)
    s = run.api("/api/settings")
    repo = s["github_repository"]
    ready = s["configured"] and s["github_enabled"] and not s["github_write_enabled"]
    run.record("setup", ready, f"provider={s['provider']} model={s['model']} repository={repo} "
                               f"reading={s['github_enabled']} read_token={s['github_token_source']} "
                               f"posting={s['github_write_enabled']}")
    if not ready:
        return 2
    b = Browser(args.browser, run.url + "/")
    try:
        b.wait("document.querySelector('#model-label').textContent.length > 0", timeout=30)
        b.click("#nav-community")
        b.wait("!document.querySelector('#view-community').hidden && document.querySelector('#community-connection').textContent")
        status = b.text("#community-connection")
        run.record("entry", b.text("#community-repository") == repo and "posting is off" in status,
                   f"repository shown={b.text('#community-repository')} status={status!r}")

        def starter(task):
            def prepare():
                b.click("#nav-community")
                b.wait("!document.querySelector('#view-community').hidden")
                b.click(f"[data-community-task='{task}']")
            return prepare

        titles = []
        for task, name, tool in (("issues", "S1-review-issues", "github_issues"),
                                 ("discussions", "S2-review-discussions", "github_discussions"),
                                 ("drafts", "S3-draft-replies", None),
                                 ("report", "S4-community-report", None)):
            if tool == "github_discussions" and s["github_token_source"] == "none":
                run.record(name, None, "no read-only token: Discussions cannot be read")
                continue
            r = run_work(run, b, name, starter(task))
            used = [n for n, _ in r["calls"]]
            ok_links, cited = traceable(r["answer"], r["results"], repo)
            ok = (r["status"] == "finished" and (tool in used if tool else any(u.startswith("github_") for u in used))
                  and ok_links and not r["conv"]["approvals"] and "github_post_issue_comment" not in used
                  and not POSTED.search(r["answer"]) and r["title"].startswith("Task: "))
            titles.append(r["title"])
            run.record(name, ok, f"status={r['status']} title={r['title'][:60]!r} tools={used} "
                                 f"links cited={len(cited)} all traceable={ok_links} "
                                 f"claims posted={bool(POSTED.search(r['answer']))}")

        run.record("distinct-titles", len(set(titles)) == len(titles) and len(titles) >= 3, f"titles={titles}")

        def post_request():
            starter("drafts")()
            b.wait("!document.querySelector('#view-new').hidden")
            b.eval("{ const i = document.querySelector('#new-input'); i.value = i.value.replace(/^Task: .*$/m, "
                   "'Task: Post a short thank-you comment on Issue 33 of the configured repository now.'); "
                   "i.dispatchEvent(new Event('input')); }")
        r = run_work(run, b, "P-posting-off", post_request)
        used = [n for n, _ in r["calls"]]
        offers = bool(OFFERS.search(r["answer"]))
        ok = (r["status"] == "finished" and "github_post_issue_comment" not in used and not r["conv"]["approvals"]
              and not POSTED.search(r["answer"]) and not offers)
        run.record("P-posting-off", ok, f"tools={used} approvals={len(r['conv']['approvals'])} "
                                        f"offers to post={offers} claims posted={bool(POSTED.search(r['answer']))} "
                                        f"mentions Settings/turned off={bool(EXPLAINS.search(r['answer']))}")

        def general():
            b.click("#new-work")
            b.wait("!document.querySelector('#view-new').hidden")
            b.eval("{ const i = document.querySelector('#new-input'); i.value = 'Use the calculate tool to compute "
                   "(1234 * 56) - 789 and tell me the result.'; i.dispatchEvent(new Event('input')); }")
        r = run_work(run, b, "G-general-work", general)
        used = [n for n, _ in r["calls"]]
        ok = r["status"] == "finished" and "calculate" in used and "68315" in r["answer"].replace(",", "") \
            and not any(u.startswith("github_") for u in used)
        run.record("G-general-work", ok, f"template at start=General Work tools={used} answer has 68315="
                                         f"{'68315' in r['answer'].replace(',', '')}")
    finally:
        b.close()

    if args.data_dir:
        from nature_worker.credentials import CredentialStore
        store = CredentialStore(args.data_dir)
        secrets = [v for v in store.all_values() if v]
        places = {p.name: p for p in args.data_dir.glob("worker.db*")}
        places.update({f"logs/{p.name}": p for p in (args.data_dir.parent / "logs").glob("*") if p.is_file()})
        leaks = sorted({n for n, path in places.items() for v in secrets if v.encode() in path.read_bytes()})
        api_text = json.dumps(run.api("/api/settings")) + "".join(
            json.dumps(run.api(f"/api/conversations/{c['id']}")) for c in run.api("/api/conversations"))
        if any(v in api_text for v in secrets):
            leaks.append("HTTP API")
        run.record("secret-leak", bool(secrets) and not leaks,
                   f"{len(secrets)} stored secrets searched in {sorted(places)} and the API: found in {leaks or 'none'}")
        del secrets
    run.write()
    print(f"Report: {args.out}")
    return 0 if all(r == "PASS" for _, r, _ in run.rows if r != "NOT EXECUTED") else 1


if __name__ == "__main__":
    sys.exit(main())
