# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Task page regression tests: the page script and the server's Task view must agree.

Real Task #77 failed to render ("ex is undefined") because the installed
service kept running old code while new UI files were served from disk. These
tests pin the response shape the page reads, for #77's recorded structure and
the other Task states, and pin the two fixes (UI snapshot, installer restart).
"""
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import nature_worker.platform_client.server as server
from nature_worker.platform_client.server import ClientSettings, create_app

from platform_fake import USER, FakePlatform

APP_JS = (Path(server.__file__).parent / "static" / "app.js").read_text()
INSTALLER = Path(__file__).parents[1] / "scripts" / "platform-client-linux-install.sh"


def iso(h, m, s, day=26):
    return f"2026-10-{day:02d}T{h:02d}:{m:02d}:{s:02d}.000000Z"


def update(status, saved_at):
    return {"status": status, "completed": "c", "important_work_product": "w", "remaining": "r",
            "dependency": "d", "next_action": "n", "user_action": "decide Approval 0b0b0b0b",
            "saved_at": saved_at}


def detail(n, **kw):
    base = {"work_pending": False, "current_update": None, "latest_work_failed": False,
            "latest_work_interrupted": False, "task_number": n, "title": f"Task {n}", "status": "open",
            "last_activity_at": iso(2, 51, 23), "goal": "g", "agent_name": "Marketing Analyst",
            "module_name": "Marketing", "work_status": None, "user_action": None,
            "history": [{"role": "user", "content": "request", "channel": "portal",
                         "created_at": iso(0, 1, 0), "platform_notice": False, "agent_name": None},
                        {"role": "agent", "content": "APPROVAL REQUESTED", "channel": "portal",
                         "created_at": iso(2, 51, 23), "platform_notice": True,
                         "agent_name": "Marketing Analyst"}],
            "can_continue_in_chat": True}
    return {**base, **kw}


def period(status, start, end, reason=None, trigger="not_recorded"):
    return {"started_at": start, "ended_at": end, "trigger": trigger, "status": status,
            "failure_reason": reason, "agent_name": "Marketing Analyst",
            "results": [{"result_id": "r-1", "kind": "task_update", "title": None, "created_at": start}]}


def approval(aid, status, can_decide, title):
    return {"approval_id": aid, "reference": f"Approval {aid[:8]}", "task_number": 77, "title": title,
            "status": status, "scope_kind": "action", "proposal": "p", "requested_by": "Marketing Analyst",
            "requested_at": iso(0, 54, 51), "expires_at": iso(0, 54, 51, day=29),
            "decided_at": iso(2, 36, 33) if status != "pending" else None, "comment": None, "uses": 0,
            "can_decide": can_decide, "details": None, "conditions": None,
            "actions": [{"tool": "External Email", "binding": "exact", "arguments": {}, "allowed": None,
                         "uses": 0, "max_uses": 1, "fingerprint": "f",
                         "email": {"sender_name": None, "sender_address": None, "sender_note": None,
                                   "recipient": "someone@example.test", "recipient_note": None,
                                   "subject": "s", "body": "b", "attachments": []}}]}


RESULTS = [{"result_id": "r-1", "kind": "update", "title": "Report", "summary": "s",
            "agent_name": "Marketing Analyst", "created_at": iso(0, 57, 52)}]
FILES = [{"file_id": "f-1", "file_name": "prospects.xlsx", "content_type": "application/vnd.ms-excel",
          "size_bytes": 15832, "sha256": "a" * 64, "version": 1, "agent_name": "Marketing Analyst",
          "updated_at": iso(0, 57, 52)}]


def recorded(task, periods, approvals=()):
    return {"detail": task, "work_history": {"task_number": task["task_number"], "periods": periods,
                                             "updates": []},
            "approvals": list(approvals), "results": RESULTS, "files": FILES}


# Task #77 as the real Platform returned it at 2026-10-26T03:02Z (structure, statuses, timestamps).
TASK_77 = recorded(
    detail(77, latest_work_failed=True, work_status="DECISION NEEDED", user_action="decide Approval 0b0b0b0b",
           current_update=update("DECISION NEEDED", iso(0, 57, 52))),
    [period("failed", iso(2, 36, 33), iso(2, 51, 23), "user_approval_required:"),
     period("completed", iso(0, 39, 18), iso(1, 18, 56), trigger="daily_work_review")],
    [approval("0a0a0a0a-0000-4000-8000-000000000001", "pending", True, "Assign External Work: proposed action"),
     approval("0b0b0b0b-0000-4000-8000-000000000002", "approved", False, "Approve single send"),
     approval("0c0c0c0c-0000-4000-8000-000000000003", "expired", False, "Approve single send")])

STATES = {
    77: ("stopped", TASK_77),
    1: ("running", recorded(detail(1, work_pending=True, current_update=update("WORKING", iso(2, 40, 0))),
                            [period("pending", iso(2, 36, 33), None)])),
    2: ("queued", recorded(detail(2, work_pending=True),
                           [period("completed", iso(0, 1, 0), iso(0, 5, 0))])),
    3: ("stopped", recorded(detail(3, status="completed", work_status="COMPLETED WORK"),
                            [period("completed", iso(0, 1, 0), iso(0, 9, 0))])),
    4: ("none", recorded(detail(4), [])),
}


@pytest.fixture
def pc(tmp_path):
    fake = FakePlatform()
    fake.recorded.update({n: rec for n, (_state, rec) in STATES.items()})
    settings = ClientSettings(data_dir=tmp_path / "pc", platform_origin="https://platform.example",
                              standalone_data_dir=tmp_path / "sa")
    with TestClient(create_app(settings, transport=fake.transport(), verify_startup=False),
                    base_url="http://127.0.0.1") as client:
        r = client.post("/api/login", json={"email": USER["email"], "password": "correct horse"})
        assert r.status_code == 200
        yield fake, client


def _function(name: str) -> str:
    start = APP_JS.index(f"function {name}(")
    following = re.search(r"\n(async )?function ", APP_JS[start + 1:])
    return APP_JS[start:start + 1 + following.start()] if following else APP_JS[start:]


def _reads(source: str, var: str) -> set[str]:
    return set(re.findall(rf"(?<![\w.]){re.escape(var)}\.(\w+)", source))


def test_task_page_reads_only_fields_the_server_provides(pc):
    _fake, client = pc
    views = {n: client.get(f"/api/tasks/{n}").json() for n in STATES}
    render, load = _function("renderProgress"), _function("loadTask")
    progress = [v["progress"] for v in views.values()]

    assert _reads(render + load, "p") <= set.intersection(*(set(p) for p in progress))
    assert _reads(load, "view") <= set.intersection(*(set(v) for v in views.values()))
    assert _reads(load, "t") <= set.intersection(*(set(v["task"]) for v in views.values()))
    assert _reads(render, "dec") <= set.intersection(*(set(p["decision"]) for p in progress))
    executions = [p["execution"] for p in progress]
    assert {"state", "agent_name"} <= set.intersection(*(set(e) for e in executions))
    assert _reads(render, "ex") <= set.union(*(set(e) for e in executions))
    assert _reads(render, "last") <= set(views[77]["progress"]["execution"]["last_period"])
    assert _reads(render, "u") <= set(views[77]["progress"]["agent_report"])
    assert _reads(render, "p.lifecycle") <= set(progress[0]["lifecycle"])


@pytest.mark.parametrize("n", sorted(STATES))
def test_each_task_state_is_presented_from_platform_facts(pc, n):
    _fake, client = pc
    expected, rec = STATES[n]
    view = client.get(f"/api/tasks/{n}").json()
    p = view["progress"]
    assert p["execution"]["state"] == expected
    assert p["lifecycle"]["closed"] is (rec["detail"]["status"] == "completed")
    assert p["active"] is (expected in ("running", "queued"))
    if expected == "running":
        assert p["execution"]["started_at"] == rec["work_history"]["periods"][0]["started_at"]
    else:
        assert "elapsed_seconds" not in p["execution"]
    # The page's other sections come straight from the Platform responses.
    assert view["approvals"] == rec["approvals"]
    assert view["work_history"] == rec["work_history"]
    assert view["task"]["history"] == rec["detail"]["history"]
    assert client.get(f"/api/tasks/{n}/results").json() == RESULTS
    assert client.get(f"/api/tasks/{n}/files").json() == FILES


def test_task_77_shows_stopped_run_new_approval_and_stale_report_without_changing_anything(pc):
    fake, client = pc
    p = client.get("/api/tasks/77").json()["progress"]
    last = p["execution"]["last_period"]
    assert (last["status"], last["failure_reason"], last["duration_seconds"]) == (
        "failed", "user_approval_required:", 890)
    assert [a["reference"] for a in p["decision"]["pending_approvals"]] == ["Approval 0a0a0a0a"]
    assert p["decision"]["yours_to_decide"] == 1
    assert p["agent_report"]["stale"] is True and not p["active"]
    assert [m for m, path in fake.requests if "/tasks/" in path and m != "GET"] == []


def test_ui_is_served_from_a_startup_snapshot(tmp_path, monkeypatch):
    static = tmp_path / "static"
    shutil.copytree(Path(server.__file__).parent / "static", static)
    monkeypatch.setattr(server, "STATIC_DIR", static)
    settings = ClientSettings(data_dir=tmp_path / "pc", platform_origin="https://platform.example",
                              standalone_data_dir=tmp_path / "sa")
    client = TestClient(create_app(settings, transport=FakePlatform().transport(), verify_startup=False),
                        base_url="http://127.0.0.1")
    original = client.get("/static/app.js")
    build = client.get("/api/status").json()["ui_build"]
    (static / "app.js").write_text("// replaced on disk by a reinstall\n")
    assert client.get("/static/app.js").content == original.content
    assert client.get("/api/status").json()["ui_build"] == build
    assert original.headers["content-type"].startswith("text/javascript")
    assert client.get("/static/../server.py").status_code == 404


def test_installer_stops_service_before_replacing_code_and_restarts_after_preflight():
    script = INSTALLER.read_text()
    stop = script.index("systemctl stop nature-worker-platform-client")
    assert stop < script.index("pip\" install")
    preflight = script.index("platform_client preflight")
    restart = script.index("systemctl restart nature-worker-platform-client")
    assert preflight < restart
    assert "enable --now" not in script
