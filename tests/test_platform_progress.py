# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
from datetime import datetime, timedelta, timezone

from nature_worker.platform_progress import QUEUED, RUNNING, STOPPED, describe, poll_interval

START = datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def period(status, start=START, end=None, **kw):
    return {"started_at": iso(start), "ended_at": iso(end) if end else None, "trigger": "request",
            "status": status, "results": [], **kw}


def task(**kw):
    return {"task_number": 1, "title": "t", "status": "open", **kw}


def test_long_running_work_is_timed_from_its_own_start_and_never_judged():
    for minutes in (4, 11, 45, 600):
        p = describe(task(work_pending=True), {"periods": [period("pending")]},
                     now=START + timedelta(minutes=minutes))
        assert p["execution"]["state"] == RUNNING and p["active"] and not p["terminal"]
        assert p["execution"]["elapsed_seconds"] == minutes * 60
        assert p["execution"]["last_period"] is None


def test_accepted_work_without_a_running_period_has_no_timer():
    # work_pending, but the newest period ended long ago: nothing is running yet.
    p = describe(task(work_pending=True),
                 {"periods": [period("completed", START, START + timedelta(minutes=5))]},
                 now=START + timedelta(days=3))
    assert p["execution"]["state"] == QUEUED and p["active"]
    assert "elapsed_seconds" not in p["execution"]


def test_dimensions_are_reported_independently():
    p = describe(task(work_pending=True, work_status="DECISION NEEDED"),
                 {"periods": [period("pending")]},
                 approvals=[{"approval_id": "a", "reference": "Approval a", "title": "Send",
                             "status": "pending", "can_decide": True}])
    assert p["execution"]["state"] == RUNNING
    assert p["decision"]["yours_to_decide"] == 1
    assert p["lifecycle"] == {"status": "open", "closed": False}
    closed = describe(task(status="completed"))
    assert closed["lifecycle"]["closed"] and closed["terminal"] and not closed["active"]


def test_decided_and_foreign_approvals_are_not_yours_to_decide():
    p = describe(task(), approvals=[
        {"approval_id": "x", "status": "approved", "can_decide": False},
        {"approval_id": "y", "status": "pending", "can_decide": False},
    ])
    assert p["decision"]["yours_to_decide"] == 0
    assert [a["approval_id"] for a in p["decision"]["pending_approvals"]] == ["y"]


def test_task_77_shape_stopped_run_new_approval_and_stale_agent_update():
    """Structure of real Platform Task #77 at 2026-10-26T03:02Z (statuses/timestamps only)."""
    t = lambda h, m, s: datetime(2026, 10, 26, h, m, s, tzinfo=timezone.utc)
    detail = task(work_pending=False, latest_work_failed=True, work_status="DECISION NEEDED",
                  user_action="DECISION: decide Approval 0b0b0b0b",
                  current_update={"status": "DECISION NEEDED", "saved_at": iso(t(0, 57, 52)),
                                  "user_action": "DECISION: decide Approval 0b0b0b0b"})
    history = {"periods": [
        period("failed", t(2, 36, 33), t(2, 51, 23), failure_reason="user_approval_required:"),
        period("completed", t(0, 39, 18), t(1, 18, 56)),
    ]}
    approvals = [
        {"approval_id": "0a0a0a0a", "reference": "Approval 0a0a0a0a", "title": "Assign External Work",
         "status": "pending", "can_decide": True, "expires_at": iso(t(2, 51, 23) + timedelta(days=3))},
        {"approval_id": "0b0b0b0b", "reference": "Approval 0b0b0b0b", "status": "approved",
         "can_decide": False},
    ]
    p = describe(detail, history, approvals=approvals, now=t(3, 2, 0))
    assert p["execution"]["state"] == STOPPED and not p["active"]
    last = p["execution"]["last_period"]
    assert (last["status"], last["failure_reason"], last["duration_seconds"]) == (
        "failed", "user_approval_required:", 890)
    assert p["execution"]["latest_work_failed"] is True
    assert [a["approval_id"] for a in p["decision"]["pending_approvals"]] == ["0a0a0a0a"]
    assert p["agent_report"]["stale"] is True  # saved 00:57, a run started 02:36


def test_agent_report_not_stale_when_saved_after_latest_run_started():
    p = describe(task(current_update={"status": "WORKING", "saved_at": iso(START + timedelta(minutes=3))}),
                 {"periods": [period("pending")]})
    assert p["agent_report"]["stale"] is False


def test_polling_slows_but_never_stops():
    assert poll_interval(0) == 3.0
    assert poll_interval(5 * 60) == 10.0
    assert poll_interval(10 * 3600) == 30.0
