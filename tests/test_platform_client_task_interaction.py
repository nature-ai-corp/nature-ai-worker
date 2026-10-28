# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Task interaction through the existing Client Interface only (Windows UI acceptance, Task #78):
reading a Task's details, work files, saved Task Updates and communications changes nothing;
Continue in Chat opens only the conversation that continues that Task; a response is posted in
that conversation and never starts a new Task."""
import time

import pytest
from fastapi.testclient import TestClient

from nature_worker.platform_client.server import ClientSettings, create_app

from platform_fake import USER, FakePlatform
from test_platform_client_task_view import APP_JS, FILES, RESULTS, _function, _reads, detail, iso, period

# Shaped like the Portal's Task #78 page (details, Agent summary, work files, saved updates,
# work history, communications on two channels with a Platform notice); not its real content.
TASK_78 = {
    "detail": detail(78, goal="Prepare the quarterly prospect list", work_status="WAITING FOR YOU",
                     user_action="Confirm the regions to include",
                     history=[{"role": "user", "content": "Please prepare the list", "channel": "portal",
                               "created_at": iso(1, 0, 0), "platform_notice": False, "agent_name": None},
                              {"role": "agent", "content": "Draft attached; which regions?", "channel": "email",
                               "created_at": iso(1, 30, 0), "platform_notice": False,
                               "agent_name": "Marketing Analyst"},
                              {"role": "agent", "content": "Work paused until you answer", "channel": "portal",
                               "created_at": iso(1, 31, 0), "platform_notice": True, "agent_name": None}]),
    "work_history": {"task_number": 78,
                     "periods": [period("completed", iso(1, 0, 5), iso(1, 30, 0))],
                     "updates": [{"status": "WAITING FOR YOU", "completed": "draft list", "dependency": "",
                                  "important_work_product": "prospects.xlsx", "remaining": "final list",
                                  "next_action": "filter by region", "user_action": "Confirm the regions",
                                  "has_work_product": True, "result_id": "r-1", "saved_at": iso(1, 30, 0),
                                  "current": True},
                                 {"status": "WORKING", "completed": "", "dependency": "", "remaining": "all",
                                  "important_work_product": "", "next_action": "collect", "user_action": "",
                                  "has_work_product": False, "result_id": "r-0", "saved_at": iso(1, 5, 0),
                                  "current": False}]},
    "approvals": [], "results": RESULTS, "files": FILES,
}


@pytest.fixture
def pc(tmp_path):
    fake = FakePlatform()
    fake.recorded[78] = TASK_78
    fake.recorded[79] = {**TASK_78, "detail": detail(79, status="completed", can_continue_in_chat=False)}
    settings = ClientSettings(data_dir=tmp_path / "pc", platform_origin="https://platform.example",
                              standalone_data_dir=tmp_path / "sa")
    app = create_app(settings, transport=fake.transport(), verify_startup=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/login", json={"email": USER["email"], "password": "correct horse"}).status_code == 200
        yield fake, client, app


def changes(fake):
    return [(m, p) for m, p in fake.requests if m != "GET" and not p.startswith("/api/v1/auth/")]


def wait(client, sid):
    for _ in range(200):
        s = client.get(f"/api/submissions/{sid}").json()
        if s["state"] != "running":
            return s
        time.sleep(0.01)
    raise AssertionError("submission did not finish")


def test_reading_task_78_changes_nothing_and_shows_every_contract_section(pc):
    fake, client, _ = pc
    view = client.get("/api/tasks/78").json()
    assert client.get("/api/tasks/78/results").json() == RESULTS
    assert client.get("/api/tasks/78/files").json() == FILES
    assert client.get("/api/tasks/78/files/f-1").status_code in (200, 404)  # recorded Tasks have no content
    assert changes(fake) == []
    t = view["task"]
    assert (t["goal"], t["work_status"], t["user_action"]) == (
        "Prepare the quarterly prospect list", "WAITING FOR YOU", "Confirm the regions to include")
    assert [u["current"] for u in view["work_history"]["updates"]] == [True, False]
    assert {h["channel"] for h in t["history"]} == {"portal", "email"}
    assert any(h["platform_notice"] for h in t["history"])
    # Lifecycle, Agent execution and the user's decisions stay separate facts.
    p = view["progress"]
    assert p["lifecycle"]["closed"] is False and p["execution"]["state"] == "stopped"
    assert p["decision"]["pending_approvals"] == []


def test_page_reads_only_task_and_history_fields_the_contract_provides(pc):
    _fake, client, _ = pc
    view = client.get("/api/tasks/78").json()
    assert _reads(_function("renderDetails"), "t") <= set(view["task"])
    history = _function("renderHistory")
    assert _reads(history, "history") <= set(view["work_history"])
    assert _reads(history, "u") <= set(view["work_history"]["updates"][0])
    assert _reads(history, "p") <= set(view["work_history"]["periods"][0])
    assert _reads(_function("renderMessages"), "m") <= (
        set(view["task"]["history"][0]) | {"role", "executing_agent_name", "agent_name", "created_at", "content"})


def test_continue_in_chat_opens_only_the_conversation_of_that_task(pc):
    fake, client, _ = pc
    conv = client.post("/api/tasks/78/continue").json()
    assert conv["task_id"] == "78"
    assert client.get(f"/api/conversations/{conv['id']}").json()["task"]["task_number"] == 78
    assert ("POST", "/api/v1/conversations") not in fake.requests  # no new, generic conversation
    assert client.post("/api/tasks/78/continue").json()["id"] == conv["id"]


def test_a_conversation_of_another_task_is_never_opened(pc):
    fake, client, _ = pc
    fake.recorded[80] = {**TASK_78, "detail": detail(80)}
    fake.continue_elsewhere[78] = 80
    r = client.post("/api/tasks/78/continue")
    assert r.status_code == 409 and "Task #80, not Task #78" in r.json()["detail"]
    r = client.post("/api/tasks/78/respond", json={"content": "go on"})
    assert r.status_code == 409
    assert not any(p.endswith("/messages") and m == "POST" for m, p in fake.requests)


def test_a_response_is_posted_in_the_tasks_conversation_and_creates_no_task(pc):
    fake, client, _ = pc
    r = client.post("/api/tasks/78/respond", json={"content": "Include EMEA and APAC"})
    assert r.status_code == 200 and r.json()["task_number"] == 78
    s = wait(client, r.json()["submission_id"])
    assert (s["state"], s["task_number"], s["expected_task"], s["error"]) == ("done", 78, 78, None)
    posted = [p for m, p in fake.requests if m == "POST" and p.endswith("/messages")]
    assert posted == [f"/api/v1/conversations/{r.json()['conversation_id']}/messages"]
    assert ("POST", "/api/v1/conversations") not in fake.requests and fake.tasks == {}
    assert fake.conversations[r.json()["conversation_id"]]["task_id"] == "78"
    history = client.get("/api/tasks/78").json()["task"]["history"]
    assert history[-1]["content"] == "Include EMEA and APAC"


def test_a_response_the_platform_files_under_a_new_task_is_reported(pc):
    fake, client, _ = pc
    fake.new_task_on_response = True
    r = client.post("/api/tasks/78/respond", json={"content": "Include EMEA"})
    s = wait(client, r.json()["submission_id"])
    assert s["state"] == "error" and "a new Task #1, not Task #78" in s["error"]


def test_a_closed_task_takes_no_response_and_no_message_is_sent(pc):
    fake, client, _ = pc
    r = client.post("/api/tasks/79/respond", json={"content": "more"})
    assert r.status_code == 409 and "cannot take a response" in r.json()["detail"]
    assert changes(fake) == []
    assert client.post("/api/tasks/78/respond", json={"content": "   "}).status_code == 422


def test_responses_are_refused_while_stopping_for_an_upgrade(pc):
    fake, client, app = pc
    app.state.draining = True
    assert client.post("/api/tasks/78/respond", json={"content": "x"}).status_code == 503
    assert changes(fake) == []


def test_approvals_stay_the_only_way_to_decide(pc):
    """A response is a message, never an approval decision."""
    start = APP_JS.index('$("task-respond").addEventListener')
    respond = APP_JS[start:APP_JS.index("\n});", start)]
    assert "/approvals/" not in respond and "/respond" in respond
