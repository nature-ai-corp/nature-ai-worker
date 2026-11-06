# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""The durable outbox for approval-gated GitHub writes: states, invariants and failure injection.

No network: these tests exercise the SQLite record that must make a remote write
at-most-once, including a real process crash between ``attempt_started`` and the send.
"""

import json
import sqlite3
import subprocess
import sys
import textwrap
import threading

import pytest

from nature_worker.db import BASE_SCHEMA, Database
from nature_worker.github_outbox import Outbox, canonical, sha256

BODY = "Thanks for the report. Could you share the Worker log?"


def snapshot(number=99, body=BODY, **extra):
    arguments = {"issue_number": number, "body": body}
    return {"tool_name": "github_post_issue_comment", "operation": "issue_comment",
            "repository": "nature-ai-corp/nature-ai-worker", "issue_number": number,
            "issue_url": f"https://github.com/nature-ai-corp/nature-ai-worker/issues/{number}",
            "comment_body": body, "comment_sha256": sha256(body), "actor": "maintainer",
            "token_fingerprint": "0123456789abcdef", "arguments": arguments,
            "target_updated_at": "2026-11-06T10:00:00Z", **extra}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "worker.db")
    yield d
    d.close()


@pytest.fixture
def outbox(db):
    return Outbox(db)


def proposed(db, outbox, call="call_1", **snap):
    conv = db.create_conversation()["id"]
    snap = snapshot(**snap)
    approval_id = outbox.propose(conv, call, "github_post_issue_comment", snap["arguments"], "Post", snap)
    return db.get_approval(approval_id), outbox.by_approval(approval_id)


def raw(db, sql, params=()):
    with db._lock:
        db._conn.execute(sql, params)
        db._conn.commit()


# -- creation --------------------------------------------------------------------


def test_proposal_creates_the_approval_and_operation_together(db, outbox):
    approval, op = proposed(db, outbox)
    assert approval["status"] == "pending" and op["state"] == "proposed"
    assert op["snapshot"]["comment_body"] == BODY and op["fingerprint"] == sha256(canonical(op["snapshot"]))
    assert [e["event"] for e in outbox.events(op["id"])] == ["proposed"]


def test_a_duplicate_tool_call_is_refused_without_an_orphan_approval(db, outbox):
    approval, op = proposed(db, outbox)
    snap = snapshot()
    with pytest.raises(sqlite3.IntegrityError):
        outbox.propose(op["conversation_id"], "call_1", "github_post_issue_comment", snap["arguments"], "Post", snap)
    assert len(db.list_approvals(op["conversation_id"])) == 1


def test_commits_are_durable_on_disk(db):
    assert db._conn.execute("PRAGMA synchronous").fetchone()[0] >= 2  # FULL


def test_an_existing_v0_1_1_database_gains_the_outbox_without_a_schema_change(tmp_path):
    path = tmp_path / "worker.db"
    conn = sqlite3.connect(path)
    conn.executescript(BASE_SCHEMA)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO conversations VALUES('c1', 'kept', 1, 1)")
    conn.commit()
    conn.close()
    d = Database(path)
    tables = {r[0] for r in d._all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"github_operations", "github_operation_events"} <= tables
    assert d._all("PRAGMA user_version")[0][0] == 1 and d.get_conversation("c1")["title"] == "kept"
    d.close()


# -- invariants enforced by SQLite itself ---------------------------------------------


@pytest.mark.parametrize("path", [
    ["confirmed"], ["attempt_started"], ["outcome_unknown"], ["reconciled"], ["failed_before_send"],
    ["denied", "approved"], ["approved", "confirmed"], ["approved", "outcome_unknown"],
    ["approved", "attempt_started", "confirmed", "outcome_unknown"],
    ["approved", "attempt_started", "outcome_unknown", "confirmed"],
    ["approved", "attempt_started", "outcome_unknown", "attempt_started"],
    ["approved", "attempt_started", "rejected", "attempt_started"],
])
def test_illegal_transitions_are_refused_by_the_database(db, outbox, path):
    _, op = proposed(db, outbox)
    *legal, illegal = path
    for state in legal:
        raw(db, "UPDATE github_operations SET state = ? WHERE id = ?", (state, op["id"]))
    with pytest.raises(sqlite3.IntegrityError, match="illegal GitHub operation state transition"):
        raw(db, "UPDATE github_operations SET state = ? WHERE id = ?", (illegal, op["id"]))


@pytest.mark.parametrize("column, value", [
    ("body", "something else"), ("issue_number", 1), ("repository", "nature-ai-corp/private"),
    ("snapshot", "{}"), ("actor", "someone"), ("approval_id", "x"), ("tool_call_id", "y"),
])
def test_the_approved_snapshot_is_immutable(db, outbox, column, value):
    _, op = proposed(db, outbox)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        raw(db, f"UPDATE github_operations SET {column} = ? WHERE id = ?", (value, op["id"]))


def test_records_cannot_be_deleted_or_inserted_mid_flight(db, outbox):
    _, op = proposed(db, outbox)
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        raw(db, "DELETE FROM github_operations")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw(db, "DELETE FROM github_operation_events")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw(db, "UPDATE github_operation_events SET event = 'confirmed'")
    row = dict(db._all("SELECT * FROM github_operations")[0])
    row.update(id="other", approval_id="other", tool_call_id="other", state="attempt_started")
    with pytest.raises(sqlite3.IntegrityError, match="starts as proposed"):
        raw(db, f"INSERT INTO github_operations({', '.join(row)}) VALUES({', '.join('?' * len(row))})",
            tuple(row.values()))


def test_deleting_the_conversation_keeps_the_audit_record(db, outbox):
    _, op = proposed(db, outbox)
    db.delete_conversation(op["conversation_id"])
    assert outbox.get(op["id"])["state"] == "proposed"


def test_a_recorded_attempt_and_result_are_written_once(db, outbox):
    approval, op = proposed(db, outbox)
    outbox.decide(approval, True)
    assert outbox.transition(op["id"], "approved", "attempt_started", attempt_started_at=10.0)
    assert outbox.transition(op["id"], "attempt_started", "confirmed", comment_id=5, comment_url="u")
    with pytest.raises(sqlite3.IntegrityError, match="cannot change"):
        raw(db, "UPDATE github_operations SET comment_id = 6 WHERE id = ?", (op["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="cannot change"):
        raw(db, "UPDATE github_operations SET attempt_started_at = 11 WHERE id = ?", (op["id"],))


# -- decisions --------------------------------------------------------------------


def test_approve_then_replay_is_refused_and_recorded(db, outbox):
    approval, op = proposed(db, outbox)
    status, claimed = outbox.decide(approval, True)
    assert status == "approved" and claimed["state"] == "approved"
    assert db.get_approval(approval["id"])["status"] == "approved"
    for approve in (True, False):
        assert outbox.decide(approval, approve)[0] == "stale"
    assert outbox.get(op["id"])["state"] == "approved"
    assert [e["event"] for e in outbox.events(op["id"])] == ["proposed", "approved", "refused_replay",
                                                              "refused_replay"]


def test_deny(db, outbox):
    approval, op = proposed(db, outbox)
    assert outbox.decide(approval, False)[0] == "denied"
    assert outbox.get(op["id"])["state"] == "denied" and db.get_approval(approval["id"])["status"] == "denied"


@pytest.mark.parametrize("tamper", [
    "UPDATE approvals SET arguments = '{\"issue_number\": 99, \"body\": \"Buy cheap tokens\"}'",
    "UPDATE approvals SET arguments = '{\"issue_number\": 1, \"body\": \"" + BODY + "\"}'",
    "UPDATE approvals SET tool_call_id = 'call_other'",
    "UPDATE approvals SET tool_name = 'delete_file'",
])
def test_an_approval_that_no_longer_matches_the_snapshot_is_refused(db, outbox, tamper):
    approval, op = proposed(db, outbox)
    raw(db, tamper)
    status, after = outbox.decide(db.get_approval(approval["id"]), True)
    assert status == "refused" and after["state"] == "denied"
    assert outbox.events(op["id"])[-1]["event"] == "refused_binding"
    assert db.get_approval(approval["id"])["status"] == "denied"


def test_concurrent_approvals_claim_the_operation_once(db, outbox):
    approval, op = proposed(db, outbox)
    results, barrier = [], threading.Barrier(8)

    def approve():
        barrier.wait()
        results.append(outbox.decide(approval, True)[0])
    threads = [threading.Thread(target=approve) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["approved"] + ["stale"] * 7


def test_a_transition_from_the_wrong_state_changes_nothing(db, outbox):
    approval, op = proposed(db, outbox)
    assert not outbox.transition(op["id"], "approved", "attempt_started", attempt_started_at=1.0)
    assert outbox.get(op["id"])["state"] == "proposed"


# -- recovery after a crash -------------------------------------------------------------


def test_recovery_settles_in_flight_operations_without_retrying(db, outbox):
    a1, op1 = proposed(db, outbox, call="c1")
    a2, op2 = proposed(db, outbox, call="c2")
    a3, op3 = proposed(db, outbox, call="c3")
    outbox.decide(a1, True)                       # approved, never sent
    outbox.decide(a2, True)
    outbox.transition(op2["id"], "approved", "attempt_started", attempt_started_at=1.0)
    assert sorted(outbox.recover()) == sorted([(op1["id"], "failed_before_send"), (op2["id"], "outcome_unknown")])
    assert outbox.get(op3["id"])["state"] == "proposed"
    assert outbox.recover() == []                 # idempotent
    assert outbox.decide(a2, True)[0] == "stale"  # the old approval cannot be used again


def test_a_real_crash_between_attempt_started_and_send_becomes_outcome_unknown(tmp_path):
    """A child process records attempt_started and dies before sending (os._exit). The parent,
    like a restarted Worker, finds outcome_unknown, and the approval cannot be replayed."""
    path = tmp_path / "worker.db"
    d = Database(path)
    out = Outbox(d)
    approval, op = proposed(d, out)
    d.close()
    child = textwrap.dedent(f"""
        import os, time
        from pathlib import Path
        from nature_worker.db import Database
        from nature_worker.github_outbox import Outbox
        db = Database(Path({str(path)!r}))
        out = Outbox(db)
        approval = db.get_approval({approval['id']!r})
        status, op = out.decide(approval, True)
        assert status == "approved"
        assert out.transition(op["id"], "approved", "attempt_started", attempt_started_at=time.time())
        os._exit(17)   # the process dies where the POST would start
    """)
    proc = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 17, proc.stderr
    d = Database(path)
    out = Outbox(d)
    assert out.get(op["id"])["state"] == "attempt_started"
    assert out.recover() == [(op["id"], "outcome_unknown")]
    assert out.decide(d.get_approval(approval["id"]), True)[0] == "stale"
    assert [e["event"] for e in out.events(op["id"])] == ["proposed", "approved", "attempt_started",
                                                          "outcome_unknown", "refused_replay"]
    d.close()


def test_events_hold_no_secrets(db, outbox):
    _, op = proposed(db, outbox)
    dump = json.dumps([dict(r) for r in db._all("SELECT * FROM github_operations")])
    assert "github_pat_" not in dump and "token_fingerprint" in json.dumps(op["snapshot"])
