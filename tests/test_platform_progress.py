from datetime import datetime, timedelta, timezone

from nature_worker.platform_progress import (
    CLOSED, FAILED, IDLE, INTERRUPTED, NEEDS_YOU, WORK_COMPLETED, WORKING, describe, poll_interval,
)

START = datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc)


def period(status, ended=None, **kw):
    return {"started_at": START.isoformat(), "ended_at": ended, "trigger": "request",
            "status": status, "results": [], **kw}


def task(**kw):
    return {"task_number": 1, "title": "t", "status": "open", **kw}


def test_long_running_work_is_never_judged_a_failure():
    for minutes in (4, 11, 45, 600):
        now = START + timedelta(minutes=minutes)
        p = describe(task(work_pending=True), {"periods": [period("pending")]}, now=now)
        assert p["state"] == WORKING and p["elapsed_seconds"] == minutes * 60 and not p["terminal"]


def test_only_platform_facts_produce_failure_or_interruption():
    failed = describe(task(latest_work_failed=True),
                      {"periods": [period("failed", START.isoformat(), failure_reason="model unavailable")]})
    assert failed["state"] == FAILED and failed["failure_reason"] == "model unavailable"
    assert describe(task(latest_work_interrupted=True))["state"] == INTERRUPTED


def test_states_from_recorded_state():
    assert describe(task(status="completed"))["state"] == CLOSED
    assert describe(task(work_status="DECISION NEEDED"))["state"] == NEEDS_YOU
    assert describe(task(), approvals=[{"status": "pending", "can_decide": True}])["state"] == NEEDS_YOU
    assert describe(task(), approvals=[{"status": "pending", "can_decide": False}])["state"] == IDLE
    assert describe(task(work_status="COMPLETED WORK"))["state"] == WORK_COMPLETED
    assert describe(task())["state"] == IDLE


def test_pending_work_wins_over_an_earlier_failure():
    p = describe(task(work_pending=True, latest_work_failed=True), {"periods": [period("pending")]})
    assert p["state"] == WORKING


def test_polling_slows_but_never_stops():
    assert poll_interval(0) == 3.0
    assert poll_interval(5 * 60) == 10.0
    assert poll_interval(10 * 3600) == 30.0
