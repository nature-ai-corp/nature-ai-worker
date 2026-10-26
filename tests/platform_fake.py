# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""In-process stand-in shaped by the Client Interface contract, for unit tests.

It enforces the contract's authentication rules (session cookie, CSRF header
on mutations, logout ends the session) so Worker logic can be tested. It is
NOT the Platform and is never integration evidence.
"""
from __future__ import annotations

import json
import re
import secrets

import httpx

NOW = "2026-10-26T01:00:00Z"
USER = {"created_at": NOW, "updated_at": NOW, "id": "u-1", "tenant_id": "t-1",
        "email": "credit@example.test", "display_name": "Credit User", "department_id": "d-1",
        "business_role_ids": [], "has_platform_access": True, "has_admin_authority": False,
        "status": "active"}
TEMPLATE = {"id": "tpl-1", "name": "Credit review", "description": "Bounded credit review", "kind": "task",
            "areas": [{"slot": 1, "label": "Customer", "description": "", "type": "text", "options": [],
                       "required": True, "section": "Request"},
                      {"slot": 2, "label": "Scope", "description": "", "type": "select",
                       "options": ["Summary", "Full"], "required": False, "section": "Request"}],
            "additional_instructions": {"label": "Additional instructions", "placeholder": "", "required": False}}


def problem(status, title, detail=""):
    return httpx.Response(status, json={"type": "about:blank", "title": title, "status": status,
                                        "detail": detail}, headers={"content-type": "application/problem+json"})


class FakePlatform:
    def __init__(self):
        self.sessions: dict[str, str] = {}   # session -> csrf
        self.conversations: dict[str, dict] = {}
        self.messages: dict[str, list] = {}
        self.tasks: dict[int, dict] = {}
        # Task number -> {"detail", "work_history", "approvals", "results", "files"}:
        # responses replayed exactly as recorded, for presentation tests.
        self.recorded: dict[int, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.idempotency: dict[str, dict] = {}
        self.password = "correct horse"
        self.ignore_logout = False  # simulate a logout that answers 204 but keeps the session

    def transport(self):
        return httpx.MockTransport(self.handle)

    def _auth(self, request):
        cookie = request.headers.get("cookie", "")
        found = dict(re.findall(r"(nature_session|nature_csrf)=([^;]+)", cookie))
        session = found.get("nature_session")
        if session not in self.sessions:
            return None, problem(401, "Unauthorized", "Not authenticated")
        if request.method != "GET" and request.headers.get("x-csrf-token") != self.sessions[session]:
            return None, problem(403, "Forbidden", "CSRF token missing or wrong")
        return session, None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else None
        if (method, path) == ("POST", "/api/v1/auth/native-login"):
            if body != {"email": USER["email"], "password": self.password}:
                return problem(401, "Unauthorized", "Sign-in failed")
            session, csrf = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
            self.sessions[session] = csrf
            return httpx.Response(200, json=USER, headers=[
                ("set-cookie", f"nature_session={session}; HttpOnly; Max-Age=28800; Path=/; SameSite=lax; Secure"),
                ("set-cookie", f"nature_csrf={csrf}; Max-Age=28800; Path=/; SameSite=lax; Secure")])
        if (method, path) == ("POST", "/api/v1/auth/logout"):
            session, err = self._auth(request)
            if session and not self.ignore_logout:
                del self.sessions[session]
            return httpx.Response(204)
        session, err = self._auth(request)
        if err:
            return err
        if path == "/api/v1/auth/me":
            return httpx.Response(200, json=USER)
        if path == "/api/v1/task-templates":
            return httpx.Response(200, json=[TEMPLATE])
        if (method, path) == ("POST", "/api/v1/conversations"):
            cid = f"c-{len(self.conversations) + 1}"
            conv = {"created_at": NOW, "updated_at": NOW, "id": cid, "tenant_id": "t-1",
                    "owner_user_id": "u-1", "title": "New conversation", "status": "active",
                    "module_id": None, "task_id": None, "channel": "portal"}
            self.conversations[cid] = conv
            self.messages[cid] = []
            return httpx.Response(201, json=conv)
        if (method, path) == ("GET", "/api/v1/conversations"):
            return httpx.Response(200, json=list(self.conversations.values()))
        m = re.fullmatch(r"/api/v1/conversations/([^/]+)(/messages|/task)?", path)
        if m:
            cid, sub = m.groups()
            if cid not in self.conversations:
                return problem(404, "Not Found")
            if sub is None:
                return httpx.Response(200, json=self.conversations[cid])
            if sub == "/task":
                n = self.conversations[cid]["task_id"]
                t = self.tasks[int(n)]
                return httpx.Response(200, json={"task_number": t["task_number"], "title": t["title"],
                                                 "continued": False, "continued_from_email": False,
                                                 "work_pending": t["work_pending"], "task_status": t["status"]})
            if method == "GET":
                return httpx.Response(200, json=self.messages[cid])
            key = request.headers.get("idempotency-key")
            if key in self.idempotency:
                return httpx.Response(200, json=self.idempotency[key], headers={"Idempotent-Replayed": "true"})
            n = len(self.tasks) + 1
            self.tasks[n] = {"task_number": n, "title": body["content"][:40], "status": "open",
                             "last_activity_at": NOW, "history": [], "can_continue_in_chat": True,
                             "work_pending": True, "work_status": None, "user_action": None,
                             "body": body}
            self.conversations[cid]["task_id"] = str(n)
            msg = {"id": f"m-{n}", "tenant_id": "t-1", "conversation_id": cid, "role": "user",
                   "content": body["content"], "created_at": NOW}
            agent = dict(msg, id=f"a-{n}", role="agent", content="Working on it.")
            self.messages[cid] += [msg, agent]
            out = {"conversation": self.conversations[cid], "user_message": msg, "agent_message": agent,
                   "execution_status": "accepted", "task": {"task_number": n, "created": True},
                   "execution": {"routing_outcome": "handle_general", "routing_result": "", "agent_id": "g",
                                 "agent_name": "General", "agent_type": "general", "module_id": None,
                                 "module_name": None, "work_item_id": None, "execution_id": "e-1",
                                 "result_id": None, "delegations": []}}
            if key:
                self.idempotency[key] = out
            return httpx.Response(200, json=out)
        m = re.fullmatch(r"/api/v1/tasks/by-number/(\d+)(/.*)?", path)
        if path == "/api/v1/tasks/mine":
            items = [{k: t[k] for k in ("task_number", "title", "status", "last_activity_at", "work_pending")}
                     for t in self.tasks.values()]
            return httpx.Response(200, json={"items": items, "total": len(items), "page": 1,
                                             "page_size": 25, "modules": []})
        if m:
            n, sub = int(m.group(1)), m.group(2) or ""
            if n in self.recorded:
                part = {"": "detail", "/work-history": "work_history", "/approvals": "approvals",
                        "/results": "results", "/files": "files"}.get(sub)
                if part is None or method != "GET":
                    return problem(404, "Not Found")
                return httpx.Response(200, json=self.recorded[n][part])
            t = self.tasks.get(n)
            if t is None:
                return problem(404, "Not Found")
            if sub == "":
                return httpx.Response(200, json={k: v for k, v in t.items() if k != "body"})
            if sub == "/work-history":
                status = "pending" if t["work_pending"] else "completed"
                return httpx.Response(200, json={"task_number": n, "updates": [], "periods": [
                    {"started_at": NOW, "ended_at": None if t["work_pending"] else NOW, "trigger": "request",
                     "status": status, "agent_name": "General", "results": []}]})
            if sub == "/approvals":
                return httpx.Response(200, json=[])
            if sub == "/results":
                return httpx.Response(200, json=[] if t["work_pending"] else [
                    {"result_id": "r-1", "kind": "reply", "title": None, "summary": "Done",
                     "agent_name": "General", "created_at": NOW}])
            if sub == "/results/r-1":
                return httpx.Response(200, json={"result_id": "r-1", "kind": "reply", "title": None,
                                                 "summary": "Done", "agent_name": "General",
                                                 "created_at": NOW, "content": "Full credit summary"})
            if sub == "/files":
                return httpx.Response(200, json=[{"file_id": "f-1", "file_name": "../report.md",
                                                  "content_type": "text/markdown", "size_bytes": 5,
                                                  "sha256": "0" * 64, "version": 1, "agent_name": None,
                                                  "updated_at": NOW}])
            if sub == "/files/f-1/content":
                return httpx.Response(200, content=b"# hi\n", headers={"content-type": "text/markdown"})
            if sub == "/complete" and method == "POST":
                t["status"] = "completed"
                return httpx.Response(200, json={"created_at": NOW, "updated_at": NOW, "id": "x",
                                                 "tenant_id": "t-1", "task_number": n,
                                                 "requested_by_user_id": "u-1", "status": "completed"})
        return problem(404, "Not Found")
