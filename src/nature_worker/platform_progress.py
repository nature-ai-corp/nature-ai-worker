# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Present a Platform Task's progress from recorded Platform state only.

Three independent facts are reported separately and never merged into one
label, because they change independently:

- lifecycle: the Task is open or closed (only its User closes it);
- execution: whether an Agent work period is running now, accepted but not
  started, or stopped - and how the last one ended, verbatim;
- decision: approval requests waiting on this User.

The Agent's own Task Update (WORKING / DECISION NEEDED / ...) is a fourth,
separate item: the Agent's assessment when it was saved, flagged when a work
period started after it.

Agent work routinely takes 4-11 minutes. Elapsed time is measured only from a
running period's own ``started_at`` and is never judged: the Worker never
times out work or declares it failed on its own.
"""
from __future__ import annotations

from datetime import datetime, timezone

RUNNING = "running"
QUEUED = "queued"
STOPPED = "stopped"
NONE = "none"


def _parse(ts: str | None) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _seconds(start: datetime | None, end: datetime | None) -> int | None:
    return int((end - start).total_seconds()) if start and end else None


def describe(task: dict, history: dict | None = None, *, approvals: list | None = None,
             now: datetime | None = None) -> dict:
    """Summarize ``TaskDetail`` + ``TaskWorkHistory`` + ``TaskApprovalView`` list."""
    now = now or datetime.now(timezone.utc)
    periods = (history or {}).get("periods") or []
    latest = periods[0] if periods else None  # newest first, per the contract
    started = _parse(latest.get("started_at")) if latest else None
    ended = _parse(latest.get("ended_at")) if latest else None

    if latest and latest.get("status") == "pending" and ended is None:
        execution = {"state": RUNNING, "started_at": latest.get("started_at"),
                     "elapsed_seconds": _seconds(started, now)}
    elif task.get("work_pending"):
        # Accepted, but no running period yet: nothing to time.
        execution = {"state": QUEUED}
    elif latest:
        execution = {"state": STOPPED}
    else:
        execution = {"state": NONE}
    execution["agent_name"] = latest.get("agent_name") if latest else None
    execution["last_period"] = None if not latest or execution["state"] == RUNNING else {
        "status": latest.get("status"),
        "failure_reason": latest.get("failure_reason"),
        "trigger": latest.get("trigger"),
        "started_at": latest.get("started_at"),
        "ended_at": latest.get("ended_at"),
        "duration_seconds": _seconds(started, ended),
    }
    execution["latest_work_failed"] = bool(task.get("latest_work_failed"))
    execution["latest_work_interrupted"] = bool(task.get("latest_work_interrupted"))

    pending = [a for a in approvals or [] if a.get("status") == "pending"]
    decision = {
        "pending_approvals": [
            {k: a.get(k) for k in ("approval_id", "reference", "title", "requested_at",
                                   "expires_at", "can_decide")}
            for a in pending
        ],
        "yours_to_decide": sum(1 for a in pending if a.get("can_decide")),
    }

    update = task.get("current_update") or None
    saved = _parse(update.get("saved_at")) if update else None
    agent_report = None if not update else {
        **update,
        "stale": bool(saved and started and started > saved),
    }

    closed = task.get("status") == "completed"
    return {
        "lifecycle": {"status": task.get("status"), "closed": closed},
        "execution": execution,
        "decision": decision,
        "agent_report": agent_report,
        "active": execution["state"] in (RUNNING, QUEUED),
        "terminal": closed,
    }


def poll_interval(elapsed_seconds: float) -> float:
    """Seconds until the next progress read; polling never stops by itself."""
    if elapsed_seconds < 60:
        return 3.0
    if elapsed_seconds < 15 * 60:
        return 10.0
    return 30.0
