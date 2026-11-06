# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Bind only Platform contract operations to the HTTP client.

The real shared S-C contract is supplied by the Platform, not guessed by the
Worker. No local enterprise Agent or business API is created: every method
here is one Client Interface operation, and every decision (routing, Task
creation, authorization, approvals) is the Platform's.
"""
from __future__ import annotations

import secrets

from .platform_contract import PlatformClientContract
from .platform_http import (
    PlatformHttpClient, PlatformInterfaceError, PlatformResponse, SessionCookies,
    SessionExpired, session_from_response,
)
from .platform_session import PlatformSessionError

# Agent work may take 4-11 minutes; a submission may be answered only when it
# finishes, so its read timeout must comfortably exceed that.
SUBMISSION_READ_TIMEOUT = 20 * 60.0


def new_idempotency_key() -> str:
    return "nw-" + secrets.token_urlsafe(24)


class PlatformOperations:
    def __init__(self, client: PlatformHttpClient, contract: PlatformClientContract):
        self.client = client
        self.contract = contract

    def call(self, operation: str, *, session: SessionCookies | None = None,
             path: dict | None = None, query: dict | None = None,
             payload: dict | None = None, idempotency_key: str | None = None,
             binary: bool = False, read_timeout: float | None = None) -> PlatformResponse:
        route = self.contract.resolve(operation)
        if route.authenticated and session is None:
            raise PlatformInterfaceError("Operation requires a Platform session")
        return self.client.request(
            route, session=session, path=path, query=query, payload=payload,
            idempotency_key=idempotency_key, binary=binary, read_timeout=read_timeout,
        )

    def execute(self, operation: str, **kwargs) -> object:
        return self.call(operation, **kwargs).data


class CookieSessionTransport:
    """``PlatformTransport`` for native email/password login.

    The password is offered for exactly one ``authenticate`` call and dropped
    immediately; it is never stored. Tokens are ``SessionCookies.to_token``.
    """

    def __init__(self, ops: PlatformOperations):
        self.ops = ops
        self._offer: tuple[str, str] | None = None
        self.user: dict | None = None

    def offer_password(self, email: str, password: str) -> None:
        self._offer = (email, password)

    def withdraw(self) -> None:
        self._offer = None

    def authenticate(self) -> str:
        offer, self._offer = self._offer, None
        if offer is None:
            raise PlatformSessionError("No sign-in credentials offered")
        response = self.ops.call("nativeLogin", payload={"email": offer[0], "password": offer[1]})
        del offer
        if not isinstance(response.data, dict) or not response.data.get("id"):
            raise PlatformSessionError("Platform sign-in returned no User")
        self.user = response.data
        return session_from_response(response).to_token()

    def validate_session(self, token: str) -> bool:
        me = self.ops.execute("getCurrentUser", session=SessionCookies.from_token(token))
        return (isinstance(me, dict) and self.user is not None and
                me.get("id") == self.user.get("id") and me.get("has_platform_access") is True)

    def revoke_session(self, token: str) -> bool:
        cookies = SessionCookies.from_token(token)
        if self.ops.call("logout", session=cookies).status != 204:
            return False
        # Confirm on the server: the old cookies must no longer authenticate.
        try:
            self.ops.call("getCurrentUser", session=cookies)
        except SessionExpired:
            return True
        return False


class ClientInterface:
    """The Client Interface operations the Worker uses, one method per operation."""

    def __init__(self, ops: PlatformOperations):
        self.ops = ops

    def _get(self, op, session, **kw):
        return self.ops.execute(op, session=session, **kw)

    # Identity
    def me(self, s): return self._get("getCurrentUser", s)

    # Templates
    def task_templates(self, s): return self._get("listTaskTemplates", s)

    # Chat and conversations
    def conversations(self, s, scope="all"): return self._get("listConversations", s, query={"scope": scope})
    def conversation(self, s, cid): return self._get("getConversation", s, path={"conversation_id": cid})
    def conversation_task(self, s, cid): return self._get("getConversationTask", s, path={"conversation_id": cid})
    def messages(self, s, cid): return self._get("listConversationMessages", s, path={"conversation_id": cid})

    def create_conversation(self, s, *, task_id: str | None = None, idempotency_key: str):
        return self._get("createConversation", s, payload={"task_id": task_id} if task_id else {},
                         idempotency_key=idempotency_key)

    def post_message(self, s, cid, *, content: str, start_task: bool = False,
                     template_id: str | None = None, idempotency_key: str):
        payload: dict = {"content": content, "start_task": start_task}
        if template_id is not None:
            payload["template_id"] = template_id
        return self._get("postConversationMessage", s, path={"conversation_id": cid},
                         payload=payload, idempotency_key=idempotency_key,
                         read_timeout=SUBMISSION_READ_TIMEOUT)

    # Tasks, progress and completion
    def my_tasks(self, s, **filters): return self._get("listMyTaskSummaries", s, query=filters)
    def task(self, s, n): return self._get("getTaskByNumber", s, path={"task_number": n})
    def work_history(self, s, n): return self._get("getTaskWorkHistory", s, path={"task_number": n})
    def complete(self, s, n): return self._get("closeTaskByNumber", s, path={"task_number": n})
    def continue_in_conversation(self, s, n):
        return self._get("continueTaskInConversation", s, path={"task_number": n})

    # Results and files
    def results(self, s, n): return self._get("listTaskResults", s, path={"task_number": n})
    def result(self, s, n, rid):
        return self._get("getTaskResult", s, path={"task_number": n, "result_id": rid})
    def files(self, s, n): return self._get("listTaskFiles", s, path={"task_number": n})

    def download(self, s, n, file_id) -> PlatformResponse:
        return self.ops.call("downloadTaskFile", session=s, binary=True,
                             path={"task_number": n, "file_id": file_id})

    # Approvals (decided and authorized by the Platform)
    def approvals(self, s, n): return self._get("listTaskApprovals", s, path={"task_number": n})

    def decide(self, s, n, approval_id, *, approve: bool, comment: str | None = None):
        op = "approveTaskApproval" if approve else "rejectTaskApproval"
        return self._get(op, s, path={"task_number": n, "approval_id": approval_id},
                         payload={"comment": comment} if comment else {})
