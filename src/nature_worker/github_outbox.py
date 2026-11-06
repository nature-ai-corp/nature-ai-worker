# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Durable outbox for approval-gated GitHub writes (Issue #33, design in PR #35).

One operation is one remote side effect (today: one comment on one Issue) that the
user approved exactly as shown. Its record is created together with the approval
request, holds an immutable snapshot of what was approved, and moves through:

    proposed -> denied
    proposed -> approved -> failed_before_send          (refused or not connected; nothing sent)
    proposed -> approved -> attempt_started -> confirmed      (GitHub returned the comment)
                                            -> rejected       (GitHub refused it: 4xx)
                                            -> failed_before_send (connection never opened)
                                            -> outcome_unknown -> reconciled

``attempt_started`` is committed to disk before any byte is sent. Anything that
interrupts an attempt (timeout, reset, 5xx, an unreadable answer, a crash or a
restart) ends in ``outcome_unknown``, which is never retried: only a read-only
reconciliation may move it to ``reconciled``, and only on an unambiguous match.
A new comment needs a new proposal and a new approval.

SQLite triggers (``db.GITHUB_OUTBOX_DDL``) enforce the transitions, the immutable
snapshot, write-once results and the append-only event log, so a bug here cannot
silently rewrite history. Tokens are never stored here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any

from .db import Database

TERMINAL = frozenset({"denied", "failed_before_send", "confirmed", "rejected", "reconciled"})


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Outbox:
    def __init__(self, db: Database):
        self.db = db

    # -- reading ------------------------------------------------------------

    def get(self, op_id: str) -> dict | None:
        return self._one("SELECT * FROM github_operations WHERE id = ?", (op_id,))

    def by_approval(self, approval_id: str) -> dict | None:
        return self._one("SELECT * FROM github_operations WHERE approval_id = ?", (approval_id,))

    def for_conversation(self, conv_id: str) -> list[dict]:
        rows = self.db._all("SELECT * FROM github_operations WHERE conversation_id = ? ORDER BY created_at",
                            (conv_id,))
        return [self._decode(r) for r in rows]

    def events(self, op_id: str) -> list[dict]:
        rows = self.db._all("SELECT event, detail, created_at FROM github_operation_events "
                            "WHERE operation_id = ? ORDER BY id", (op_id,))
        return [dict(r) for r in rows]

    def _one(self, sql: str, params: tuple) -> dict | None:
        rows = self.db._all(sql, params)
        return self._decode(rows[0]) if rows else None

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        op = dict(row)
        op["snapshot"] = json.loads(op["snapshot"])
        return op

    # -- writing --------------------------------------------------------------

    def propose(self, conv_id: str, tool_call_id: str, tool_name: str, arguments: dict, reason: str,
                snapshot: dict) -> str:
        """Create the approval request and its operation together. Returns the approval id.

        Raises sqlite3.IntegrityError for a second proposal from the same tool call."""
        approval_id, op_id, now = uuid.uuid4().hex, uuid.uuid4().hex, time.time()
        text = canonical(snapshot)
        with self.db.transaction() as c:
            c.execute("INSERT INTO approvals(id, conversation_id, tool_call_id, tool_name, arguments, reason, "
                      "status, created_at) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?)",
                      (approval_id, conv_id, tool_call_id, tool_name, json.dumps(arguments), reason, now))
            c.execute("INSERT INTO github_operations(id, approval_id, conversation_id, tool_call_id, operation, "
                      "repository, issue_number, body, body_sha256, snapshot, fingerprint, actor, token_fingerprint, "
                      "state, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)",
                      (op_id, approval_id, conv_id, tool_call_id, snapshot["operation"], snapshot["repository"],
                       snapshot["issue_number"], snapshot["comment_body"], snapshot["comment_sha256"], text,
                       sha256(text), snapshot["actor"], snapshot["token_fingerprint"], now, now))
            self._event(c, op_id, approval_id, "proposed", None, now)
        return approval_id

    def decide(self, approval: dict, approve: bool) -> tuple[str, dict | None]:
        """Resolve a pending approval and claim its operation in one transaction.

        Returns ("approved" | "denied", operation), ("stale", operation) when the approval
        was already answered (a replay; recorded, nothing else changes), or ("refused",
        operation) when the approval no longer matches the immutable snapshot (it is then
        denied). Only an "approved" result may lead to a network write."""
        now = time.time()
        with self.db.transaction() as c:
            row = c.execute("SELECT * FROM github_operations WHERE approval_id = ?", (approval["id"],)).fetchone()
            op = self._decode(row) if row else None
            current = c.execute("SELECT * FROM approvals WHERE id = ?", (approval["id"],)).fetchone()
            if op is None or current is None:
                self._event(c, op and op["id"], approval["id"], "refused_no_operation", None, now)
                if current is not None and current["status"] == "pending":
                    c.execute("UPDATE approvals SET status = 'denied', resolved_at = ? WHERE id = ?",
                              (now, approval["id"]))
                return "refused", op
            if current["status"] != "pending" or op["state"] != "proposed":
                self._event(c, op["id"], approval["id"], "refused_replay",
                            f"approval {current['status']}, operation {op['state']}", now)
                return "stale", op
            problem = _binding_problem(op, dict(current))
            status = "approved" if approve and not problem else "denied"
            c.execute("UPDATE approvals SET status = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
                      (status, now, approval["id"]))
            c.execute("UPDATE github_operations SET state = ?, updated_at = ?, detail = ? WHERE id = ? "
                      "AND state = 'proposed'", (status, now, problem, op["id"]))
            self._event(c, op["id"], approval["id"], status if not problem else "refused_binding", problem, now)
            op.update(state=status, detail=problem)
        return ("refused" if problem else status), op

    def transition(self, op_id: str, old: str, new: str, *, detail: str | None = None, **fields) -> bool:
        """Move one operation from ``old`` to ``new`` and log it. False if it was not in ``old``."""
        now = time.time()
        allowed = {"attempt_started_at", "comment_id", "comment_url", "comment_created_at"}
        if set(fields) - allowed:
            raise ValueError(f"unexpected fields {sorted(set(fields) - allowed)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db.transaction() as c:
            cur = c.execute(f"UPDATE github_operations SET state = ?, updated_at = ?, detail = ?"
                            f"{', ' + sets if sets else ''} WHERE id = ? AND state = ?",
                            (new, now, detail, *fields.values(), op_id, old))
            if cur.rowcount != 1:
                return False
            self._event(c, op_id, None, new, detail, now)
        return True

    def record(self, op_id: str | None, event: str, detail: str | None = None, approval_id: str | None = None) -> None:
        """Log an event that does not change state (a refused replay, a reconciliation without a match)."""
        with self.db.transaction() as c:
            self._event(c, op_id, approval_id, event, detail, time.time())

    def recover(self) -> list[tuple[str, str]]:
        """At startup: settle operations a previous process left in flight.

        ``approved`` without ``attempt_started`` never sent anything. ``attempt_started``
        may have reached GitHub, so it becomes ``outcome_unknown`` and is never retried."""
        moved = []
        for op in self._all_in(("approved", "attempt_started")):
            if op["state"] == "approved":
                new, why = "failed_before_send", "The Worker stopped before sending; nothing was sent to GitHub."
            else:
                new, why = "outcome_unknown", ("The Worker stopped during the attempt; the comment may or may not "
                                               "have been published. It is not retried.")
            if self.transition(op["id"], op["state"], new, detail=why):
                moved.append((op["id"], new))
        return moved

    def _all_in(self, states: tuple[str, ...]) -> list[dict]:
        marks = ", ".join("?" for _ in states)
        return [self._decode(r) for r in self.db._all(f"SELECT * FROM github_operations WHERE state IN ({marks})",
                                                       states)]

    @staticmethod
    def _event(c: sqlite3.Connection, op_id: str | None, approval_id: str | None, event: str, detail: str | None,
               now: float) -> None:
        c.execute("INSERT INTO github_operation_events(operation_id, approval_id, event, detail, created_at) "
                  "VALUES(?, ?, ?, ?, ?)", (op_id, approval_id, event, detail, now))


def _binding_problem(op: dict, approval: dict) -> str | None:
    """Why this approval does not authorize exactly this operation, or None."""
    snap = op["snapshot"]
    try:
        arguments = json.loads(approval["arguments"]) if isinstance(approval["arguments"], str) \
            else approval["arguments"]
    except ValueError:
        arguments = None
    if approval["tool_call_id"] != op["tool_call_id"] or approval["conversation_id"] != op["conversation_id"]:
        return "The approval belongs to a different request."
    if approval["tool_name"] != snap["tool_name"]:
        return "The approval is for a different tool."
    if canonical(snap["arguments"]) != canonical(_normalized(arguments)):
        return "The approved arguments differ from the proposal."
    if sha256(op["body"]) != op["body_sha256"] or op["body"] != snap["comment_body"] \
            or snap["comment_sha256"] != op["body_sha256"]:
        return "The comment text differs from the proposal."
    if sha256(canonical(snap)) != op["fingerprint"]:
        return "The stored proposal does not match its fingerprint."
    return None


def _normalized(arguments: Any) -> Any:
    # The same normalization the proposal applied (see tools.github_write.normalize_arguments).
    from .tools.github_write import normalize_arguments
    try:
        return normalize_arguments(arguments)
    except Exception:  # noqa: BLE001 - anything unparsable simply does not match
        return None
