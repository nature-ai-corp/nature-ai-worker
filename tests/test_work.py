"""The My Work view (WKR-023): status, facts and results derived only from existing records."""

import os
import time

import pytest

from conftest import configure, conversation, new_conversation, openai_error, openai_text, openai_tool_call, send
from nature_worker.work import summarize


def work(client, conv_id):
    return conversation(client, conv_id)["work"]


@pytest.fixture
def ready(client, fake_model):
    configure(client, endpoint=fake_model.url + "/v1")
    return client


# -- status -----------------------------------------------------------------------------

def test_new_work_is_not_started(ready):
    w = work(ready, new_conversation(ready))
    assert (w["status"], w["status_label"], w["update"]) == ("not_started", "Not started", "")
    assert w["deliverables"] == [] and w["inputs"] == []


def test_finished_means_the_run_ended_with_a_reply(ready, fake_model):
    conv_id = new_conversation(ready)
    fake_model.queue(openai_text("Here is the answer."))
    send(ready, conv_id, "question")
    w = work(ready, conv_id)
    assert (w["status"], w["update"], w["instruction"]) == ("finished", "Here is the answer.", "question")


def test_provider_error_is_failed(ready, fake_model):
    conv_id = new_conversation(ready)
    fake_model.queue(openai_error(401, "Incorrect API key provided."))
    send(ready, conv_id, "do it")
    w = work(ready, conv_id)
    assert w["status"] == "failed" and "Incorrect API key" in w["update"]


def test_step_limit_is_stopped(ready, fake_model):
    configure(ready, endpoint=fake_model.url + "/v1", max_steps=1)
    conv_id = new_conversation(ready)
    fake_model.queue(openai_tool_call(("list_files", {})))
    send(ready, conv_id, "list")
    w = work(ready, conv_id)
    assert w["status"] == "stopped" and "Stopped after 1 steps" in w["update"]


def test_pending_approval_needs_approval(ready, config, fake_model):
    (config.workspace_dir / "notes.md").write_text("keep")
    conv_id = new_conversation(ready)
    fake_model.queue(openai_tool_call(("delete_file", {"path": "notes.md"})))
    send(ready, conv_id, "delete notes.md")
    w = work(ready, conv_id)
    assert w["status"] == "needs_approval" and w["update"] == "Delete file 'notes.md'"
    assert w["facts"]["approvals_pending"] == 1


def test_run_without_reply_is_interrupted(client):
    """The server stopped mid-run: the last record is the user's message or a tool result."""
    db = client.app.state.db
    conv = db.create_conversation("x")
    db.add_message(conv["id"], "user", "write the report")
    w = work(client, conv["id"])
    assert w["status"] == "interrupted" and "continue" in w["update"]
    db.add_message(conv["id"], "assistant", "", tool_calls=[{"id": "c1", "name": "list_files", "arguments": {}}])
    assert work(client, conv["id"])["status"] == "interrupted"  # a tool call without a result


def test_running_overrides_the_records():
    conv = {"id": "c", "created_at": 1.0, "updated_at": 2.0}
    msgs = [{"role": "user", "content": "x", "tool_calls": None, "meta": None, "created_at": 1.0}]
    assert summarize(conv, msgs, [], running=True)["status"] == "working"


# -- results: deliverables vs inputs -------------------------------------------------------

def _report_flow(ready, fake_model, path="report.md"):
    conv_id = new_conversation(ready)
    fake_model.queue(
        openai_tool_call(("read_file", {"path": "sales.csv"}), ("table_summary", {"path": "./sales.csv"})),
        openai_tool_call(("write_file", {"path": path, "content": "# Report\n"})),
        openai_text("Wrote the report."),
    )
    send(ready, conv_id, "Read sales.csv and write report.md")
    return conv_id


def test_written_file_is_a_deliverable_and_read_file_is_an_input(ready, config, fake_model):
    (config.workspace_dir / "sales.csv").write_text("region,revenue\nNorth,1\n")
    w = work(ready, _report_flow(ready, fake_model))
    [d] = w["deliverables"]
    assert d["path"] == "report.md" and d["action"] == "Created" and d["available"] and d["size"] == 9
    assert d["changed_since"] is False
    assert [i["path"] for i in w["inputs"]] == ["sales.csv"]  # "./sales.csv" is the same file
    assert w["facts"]["tool_calls"] == 3 and w["facts"]["agent_steps"] == 3


def test_failed_and_denied_writes_are_not_deliverables(ready, config, fake_model):
    (config.workspace_dir / "report.md").write_text("original")
    conv_id = new_conversation(ready)
    fake_model.queue(openai_tool_call(("write_file", {"path": "../outside.md", "content": "x"}),
                                      ("write_file", {"path": "report.md", "content": "new"})))
    send(ready, conv_id, "overwrite")
    approval = conversation(ready, conv_id)["approvals"][0]
    fake_model.queue(openai_text("OK, not overwritten."))
    ready.post(f"/api/approvals/{approval['id']}", json={"approve": False})
    w = work(ready, conv_id)
    assert w["deliverables"] == []
    assert w["facts"]["tool_failures"] == 1 and w["facts"]["denied"] == 1 and w["facts"]["approvals_denied"] == 1
    assert (config.workspace_dir / "report.md").read_text() == "original"


def test_approved_overwrite_is_a_deliverable(ready, config, fake_model):
    (config.workspace_dir / "report.md").write_text("original")
    conv_id = new_conversation(ready)
    fake_model.queue(openai_tool_call(("write_file", {"path": "report.md", "content": "updated"})))
    send(ready, conv_id, "update the report")
    approval = conversation(ready, conv_id)["approvals"][0]
    fake_model.queue(openai_text("Updated."))
    ready.post(f"/api/approvals/{approval['id']}", json={"approve": True})
    w = work(ready, conv_id)
    assert [(d["path"], d["action"]) for d in w["deliverables"]] == [("report.md", "Overwrote")]
    assert w["facts"]["approvals_approved"] == 1


def test_deliverable_changed_or_removed_later_is_reported(ready, config, fake_model):
    (config.workspace_dir / "sales.csv").write_text("a\n")
    conv_id = _report_flow(ready, fake_model)
    report = config.workspace_dir / "report.md"
    later = time.time() + 60
    os.utime(report, (later, later))
    assert work(ready, conv_id)["deliverables"][0]["changed_since"] is True
    report.unlink()
    [d] = work(ready, conv_id)["deliverables"]
    assert d["available"] is False and "size" not in d


def test_file_deleted_by_the_work_is_not_a_deliverable(ready, config, fake_model):
    conv_id = new_conversation(ready)
    fake_model.queue(openai_tool_call(("write_file", {"path": "tmp.md", "content": "x"})),
                     openai_tool_call(("delete_file", {"path": "tmp.md"})))
    send(ready, conv_id, "make and remove tmp.md")
    approval = conversation(ready, conv_id)["approvals"][0]
    fake_model.queue(openai_text("Done."))
    ready.post(f"/api/approvals/{approval['id']}", json={"approve": True})
    assert work(ready, conv_id)["deliverables"] == []


def test_uploaded_files_are_not_deliverables(ready, config, fake_model):
    ready.put("/api/files", params={"path": "upload.md"}, content=b"from the user")
    conv_id = new_conversation(ready)
    fake_model.queue(openai_text("Seen."))
    send(ready, conv_id, "hello")
    w = work(ready, conv_id)
    assert w["deliverables"] == [] and w["inputs"] == []


def test_results_never_expose_files_outside_the_workspace(client, config):
    """Even a record that claims a successful write outside the workspace is not shown as available."""
    secret = config.workspace_dir.parent / "secret.md"
    secret.write_text("TOP-SECRET")
    os.symlink(secret, config.workspace_dir / "link.md")
    db = client.app.state.db
    conv = db.create_conversation("x")
    calls = [{"id": f"c{i}", "name": "write_file", "arguments": {"path": p, "content": "x"}}
             for i, p in enumerate(["../secret.md", str(secret), "link.md"])]
    db.add_message(conv["id"], "assistant", "", tool_calls=calls)
    for c in calls:
        db.add_message(conv["id"], "tool", "Created.", tool_call_id=c["id"], tool_name="write_file")
    r = client.get(f"/api/conversations/{conv['id']}")
    assert all(d["available"] is False and "size" not in d for d in r.json()["work"]["deliverables"])
    assert "TOP-SECRET" not in r.text


# -- My Work list -----------------------------------------------------------------------------

def test_my_work_lists_existing_conversations_with_derived_status(ready, config, fake_model):
    (config.workspace_dir / "sales.csv").write_text("a\n")
    done = _report_flow(ready, fake_model)
    failed = new_conversation(ready)
    fake_model.queue(openai_error(500, "boom"))
    send(ready, failed, "x" * 80)
    items = ready.get("/api/work").json()
    assert [i["id"] for i in items] == [failed, done]  # newest activity first
    by_id = {i["id"]: i for i in items}
    assert by_id[done]["status"] == "finished" and by_id[done]["deliverables"] == 1
    assert by_id[failed]["status"] == "failed" and by_id[failed]["instruction"] == "x" * 80
    assert set(items[0]) == {"id", "title", "created_at", "updated_at", "status", "status_label",
                             "instruction", "deliverables", "approvals_pending"}  # nothing invented


def test_existing_conversation_api_is_unchanged(ready, fake_model):
    conv_id = new_conversation(ready)
    fake_model.queue(openai_text("hi"))
    send(ready, conv_id, "hello")
    data = conversation(ready, conv_id)
    assert {"id", "title", "messages", "approvals", "running", "work"} <= set(data)
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
