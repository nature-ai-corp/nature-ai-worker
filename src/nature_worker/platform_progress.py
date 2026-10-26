"""Present a Platform Task's progress from recorded Platform state only.

Agent work routinely takes 4-11 minutes. Elapsed time is shown, never judged:
only the Platform's own facts (``latest_work_failed``, a failed or
interrupted work period, a closed Task) change the state. The Worker never
times out work or declares it failed on its own.
"""
from __future__ import annotations

from datetime import datetime, timezone

WORKING = "working"
NEEDS_YOU = "needs_you"
WORK_COMPLETED = "work_completed"
FAILED = "failed"
INTERRUPTED = "interrupted"
IDLE = "idle"
CLOSED = "closed"

_LABELS = {
    WORKING: "Agent is working",
    NEEDS_YOU: "Waiting for you",
    WORK_COMPLETED: "Agent reports the work completed",
    FAILED: "The Platform reports the latest work failed",
    INTERRUPTED: "The Platform reports the latest work was interrupted",
    IDLE: "Open - no work running",
    CLOSED: "Task closed",
}


def _parse(ts: str | None) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def describe(task: dict, history: dict | None = None, *, approvals: list | None = None,
             now: datetime | None = None) -> dict:
    """Summarize ``TaskDetail`` (+ optional ``TaskWorkHistory`` and approvals)."""
    now = now or datetime.now(timezone.utc)
    update = task.get("current_update") or None
    periods = (history or {}).get("periods") or []
    latest = periods[0] if periods else None  # newest first, per the contract
    pending_approval = any(
        a.get("status") == "pending" and a.get("can_decide") for a in approvals or []
    )

    if task.get("status") == "completed":
        state = CLOSED
    elif task.get("work_pending") or (latest and latest.get("status") == "pending"):
        state = WORKING
    elif pending_approval or (latest and latest.get("status") == "waiting_for_approval"):
        state = NEEDS_YOU
    elif task.get("latest_work_failed"):
        state = FAILED
    elif task.get("latest_work_interrupted"):
        state = INTERRUPTED
    elif task.get("work_status") == "DECISION NEEDED" or task.get("user_action"):
        state = NEEDS_YOU
    elif task.get("work_status") == "COMPLETED WORK":
        state = WORK_COMPLETED
    else:
        state = IDLE

    started = _parse(latest.get("started_at")) if latest else None
    ended = _parse(latest.get("ended_at")) if latest else None
    elapsed = None
    if started:
        elapsed = int(((ended or now) - started).total_seconds())
    return {
        "state": state,
        "label": _LABELS[state],
        "work_status": task.get("work_status"),
        "user_action": task.get("user_action"),
        "update": update,
        "latest_period": latest,
        "elapsed_seconds": elapsed,
        "failure_reason": latest.get("failure_reason") if latest and state == FAILED else None,
        "terminal": state == CLOSED,
    }


def poll_interval(elapsed_seconds: float) -> float:
    """Seconds until the next progress read; polling never stops by itself."""
    if elapsed_seconds < 60:
        return 3.0
    if elapsed_seconds < 15 * 60:
        return 10.0
    return 30.0
