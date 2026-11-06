# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""SQLite storage for conversations, messages, approvals and settings.

API keys are never stored here (see ``credentials.py``).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,            -- user | assistant | tool | notice
    content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT,               -- JSON list for assistant messages
    tool_call_id TEXT,             -- for tool messages
    tool_name TEXT,                -- for tool messages
    meta TEXT,                     -- JSON (provider extras, error kind, ...)
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,          -- pending | approved | denied
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# The durable outbox for approval-gated GitHub writes (see github_outbox.py). The tables are
# additive and created on every open (IF NOT EXISTS), so they need no data-schema version:
# older versions ignore them, and a rollback keeps the audit record.
# The rules that keep it an audit record are enforced by SQLite itself, not only by the code:
# rows start as 'proposed', their snapshot never changes, states only move along the allowed
# transitions, a confirmed result is written once, and nothing is ever deleted. Operations are
# not tied to a conversation: deleting the conversation keeps the record of a remote side effect.
# One statement per entry, so each can also run on its own.
GITHUB_OUTBOX_DDL = [
    """CREATE TABLE IF NOT EXISTS github_operations (
    id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    operation TEXT NOT NULL,          -- issue_comment
    repository TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    snapshot TEXT NOT NULL,           -- canonical JSON of everything the user approves
    fingerprint TEXT NOT NULL,        -- SHA-256 of the snapshot
    actor TEXT NOT NULL,              -- GitHub login the token belonged to at proposal time
    token_fingerprint TEXT NOT NULL,  -- truncated SHA-256 of the token, never the token
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    attempt_started_at REAL,
    comment_id INTEGER,
    comment_url TEXT,
    comment_created_at TEXT,
    detail TEXT,
    UNIQUE (conversation_id, tool_call_id)
)""",
    """CREATE TABLE IF NOT EXISTS github_operation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT,
    approval_id TEXT,
    event TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_github_operations_conv ON github_operations(conversation_id, created_at)",
    """CREATE TRIGGER IF NOT EXISTS github_operations_insert BEFORE INSERT ON github_operations
WHEN NEW.state != 'proposed' OR NEW.attempt_started_at IS NOT NULL OR NEW.comment_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'a GitHub operation starts as proposed'); END""",
    """CREATE TRIGGER IF NOT EXISTS github_operations_immutable BEFORE UPDATE OF
id, approval_id, conversation_id, tool_call_id, operation, repository, issue_number, body, body_sha256,
snapshot, fingerprint, actor, token_fingerprint, created_at ON github_operations
BEGIN SELECT RAISE(ABORT, 'the approved GitHub operation is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS github_operations_transition BEFORE UPDATE OF state ON github_operations
WHEN NOT ((OLD.state = 'proposed' AND NEW.state IN ('approved', 'denied'))
       OR (OLD.state = 'approved' AND NEW.state IN ('attempt_started', 'failed_before_send'))
       OR (OLD.state = 'attempt_started'
           AND NEW.state IN ('confirmed', 'rejected', 'outcome_unknown', 'failed_before_send'))
       OR (OLD.state = 'outcome_unknown' AND NEW.state = 'reconciled'))
BEGIN SELECT RAISE(ABORT, 'illegal GitHub operation state transition'); END""",
    """CREATE TRIGGER IF NOT EXISTS github_operations_write_once BEFORE UPDATE OF
attempt_started_at, comment_id, comment_url, comment_created_at ON github_operations
WHEN OLD.attempt_started_at IS NOT NULL AND NEW.attempt_started_at IS NOT OLD.attempt_started_at
  OR OLD.comment_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'a recorded GitHub attempt or result cannot change'); END""",
    """CREATE TRIGGER IF NOT EXISTS github_operations_no_delete BEFORE DELETE ON github_operations
BEGIN SELECT RAISE(ABORT, 'GitHub operations are an audit record and cannot be deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS github_operation_events_no_update BEFORE UPDATE ON github_operation_events
BEGIN SELECT RAISE(ABORT, 'GitHub operation events are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS github_operation_events_no_delete BEFORE DELETE ON github_operation_events
BEGIN SELECT RAISE(ABORT, 'GitHub operation events are append-only'); END""",
]

SCHEMA = BASE_SCHEMA + "".join(statement + ";\n" for statement in GITHUB_OUTBOX_DDL)


def _loads(value: str | None) -> Any:
    return json.loads(value) if value else None


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        # A commit is on disk before it returns: the outbox records an attempt before sending it.
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One atomic, committed-on-success unit of several statements."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- settings -----------------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self._all("SELECT key, value FROM settings")}

    def set_settings(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self._exec(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    # -- conversations ------------------------------------------------------

    def create_conversation(self, title: str = "New conversation") -> dict[str, Any]:
        now = time.time()
        conv_id = uuid.uuid4().hex
        self._exec(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?, ?, ?, ?)",
            (conv_id, title, now, now),
        )
        return {"id": conv_id, "title": title, "created_at": now, "updated_at": now}

    def list_conversations(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM conversations ORDER BY updated_at DESC")
        return [dict(r) for r in rows]

    def get_conversation(self, conv_id: str) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM conversations WHERE id = ?", (conv_id,))
        return dict(rows[0]) if rows else None

    def rename_conversation(self, conv_id: str, title: str) -> None:
        self._exec("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))

    def delete_conversation(self, conv_id: str) -> None:
        self._exec("DELETE FROM conversations WHERE id = ?", (conv_id,))

    # -- messages -----------------------------------------------------------

    def add_message(
        self,
        conv_id: str,
        role: str,
        content: str = "",
        *,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        meta: dict | None = None,
    ) -> int:
        now = time.time()
        cur = self._exec(
            "INSERT INTO messages(conversation_id, role, content, tool_calls, tool_call_id, "
            "tool_name, meta, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conv_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id,
                tool_name,
                json.dumps(meta) if meta else None,
                now,
            ),
        )
        self._exec("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
        return cur.lastrowid

    def get_messages(self, conv_id: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,))
        out = []
        for r in rows:
            m = dict(r)
            m["tool_calls"] = _loads(m["tool_calls"])
            m["meta"] = _loads(m["meta"])
            out.append(m)
        return out

    # -- approvals ----------------------------------------------------------

    def add_approval(self, conv_id: str, tool_call_id: str, tool_name: str, arguments: dict, reason: str) -> str:
        approval_id = uuid.uuid4().hex
        self._exec(
            "INSERT INTO approvals(id, conversation_id, tool_call_id, tool_name, arguments, reason, "
            "status, created_at) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?)",
            (approval_id, conv_id, tool_call_id, tool_name, json.dumps(arguments), reason, time.time()),
        )
        return approval_id

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not rows:
            return None
        a = dict(rows[0])
        a["arguments"] = json.loads(a["arguments"])
        return a

    def pending_approvals(self, conv_id: str) -> list[dict[str, Any]]:
        return [a for a in self.list_approvals(conv_id) if a["status"] == "pending"]

    def list_approvals(self, conv_id: str) -> list[dict[str, Any]]:
        """All approval requests of a conversation, any status, oldest first."""
        rows = self._all("SELECT * FROM approvals WHERE conversation_id = ? ORDER BY created_at", (conv_id,))
        out = []
        for r in rows:
            a = dict(r)
            a["arguments"] = json.loads(a["arguments"])
            out.append(a)
        return out

    def resolve_approval(self, approval_id: str, status: str) -> bool:
        """Mark a pending approval as approved/denied. Returns False if it was not pending."""
        cur = self._exec(
            "UPDATE approvals SET status = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
            (status, time.time(), approval_id),
        )
        return cur.rowcount == 1
