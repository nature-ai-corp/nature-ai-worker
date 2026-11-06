# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Reliable document creation: cut-off replies are never executed, long documents are written in
bounded parts, and the Worker may revise only files it created in this conversation while they are
still exactly what it wrote. Every other change of an existing file still needs approval."""
import os

import pytest

from conftest import configure, conversation, new_conversation, openai_text, openai_tool_call, send
from nature_worker.tools import ApprovalRequired, ToolContext, execute, owned_files
from nature_worker.tools.workspace import MAX_APPEND_BYTES, MAX_WRITE_BYTES, FileChanged, Workspace


def cut_off(response):
    status, body = response
    body["choices"][0]["finish_reason"] = "length"
    return status, body


def approvals(client, conv_id):
    return [a for a in conversation(client, conv_id)["approvals"] if a["status"] == "pending"]


@pytest.fixture
def worker(client, config, fake_model):
    configure(client, endpoint=fake_model.url + "/v1")
    return client, config.workspace_dir, fake_model


def test_create_revise_and_append_a_report_without_approvals(worker):
    client, ws, model = worker
    model.queue(
        openai_tool_call(("write_file", {"path": "reports/q3.md", "content": "# Q3\n\n## Sales\n"})),
        openai_tool_call(("append_file", {"path": "reports/q3.md", "content": "North: 170\n"})),
        openai_tool_call(("write_file", {"path": "./reports/../reports/q3.md", "content": "# Q3 (revised)\n"})),
        openai_tool_call(("append_file", {"path": "reports/q3.md", "content": "## Regions\nNorth: 170\n"})),
        openai_text("Done."),
    )
    conv_id = new_conversation(client)
    assert send(client, conv_id, "write the report") == "done"
    assert (ws / "reports" / "q3.md").read_text() == "# Q3 (revised)\n## Regions\nNorth: 170\n"
    assert approvals(client, conv_id) == []
    conv = conversation(client, conv_id)
    results = [m["content"] for m in conv["messages"] if m["role"] == "tool"]
    assert results[0].startswith("Created reports/q3.md") and results[2].startswith("Overwrote reports/q3.md")
    assert [d["path"] for d in conv["work"]["deliverables"]] == ["reports/q3.md"]
    # The next message in the same conversation may still revise it (the record is in the history).
    model.queue(openai_tool_call(("append_file", {"path": "reports/q3.md", "content": "Total: 490\n"})),
                openai_text("Added."))
    assert send(client, conv_id, "add the total") == "done"
    assert (ws / "reports" / "q3.md").read_text().endswith("Total: 490\n")


@pytest.mark.parametrize("tool", ["write_file", "append_file"])
def test_a_users_existing_file_always_needs_approval(worker, tool):
    client, ws, model = worker
    (ws / "plan.md").write_text("the user's plan\n")
    model.queue(openai_tool_call((tool, {"path": "plan.md", "content": "changed\n"})))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "change plan.md") == "needs_approval"
    assert (ws / "plan.md").read_text() == "the user's plan\n"
    pending = approvals(client, conv_id)
    assert len(pending) == 1 and pending[0]["reason"].endswith("existing file 'plan.md'")
    # Approved once, it stays the user's file: the next change asks again.
    model.queue(openai_tool_call((tool, {"path": "plan.md", "content": "again\n"})))
    r = client.post(f"/api/approvals/{pending[0]['id']}", json={"approve": True})
    assert r.json()["status"] == "needs_approval"
    assert "changed\n" in (ws / "plan.md").read_text() and "again" not in (ws / "plan.md").read_text()


def _created_then(worker, change):
    """The Worker creates notes.md; ``change`` alters it outside the Worker; then it revises it."""
    client, ws, model = worker
    model.queue(openai_tool_call(("write_file", {"path": "notes.md", "content": "draft\n"})), openai_text("ok"))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "draft notes") == "done"
    change(ws)
    model.queue(openai_tool_call(("write_file", {"path": "notes.md", "content": "revised\n"})))
    return client, ws, conv_id, send(client, conv_id, "revise notes")


def test_a_worker_file_the_user_edited_needs_approval_again(worker):
    client, ws, conv_id, status = _created_then(worker, lambda ws: (ws / "notes.md").write_text("user edit\n"))
    assert status == "needs_approval" and (ws / "notes.md").read_text() == "user edit\n"


def test_a_worker_file_replaced_by_another_file_with_the_same_content_needs_approval(worker):
    def replace(ws):
        (ws / "copy.md").write_text("draft\n")        # same bytes, a different file (inode)
        os.replace(ws / "copy.md", ws / "notes.md")
    client, ws, conv_id, status = _created_then(worker, replace)
    assert status == "needs_approval" and (ws / "notes.md").read_text() == "draft\n"


def test_a_worker_file_replaced_by_a_symlink_to_a_user_file_needs_approval(worker):
    def link(ws):
        (ws / "precious.txt").write_text("user data\n")
        (ws / "notes.md").unlink()
        os.symlink(ws / "precious.txt", ws / "notes.md")
    try:
        client, ws, conv_id, status = _created_then(worker, link)
    except OSError:
        pytest.skip("symbolic links not permitted")
    assert status == "needs_approval" and (ws / "precious.txt").read_text() == "user data\n"


def test_the_record_is_scoped_to_its_conversation(worker):
    client, ws, model = worker
    model.queue(openai_tool_call(("write_file", {"path": "notes.md", "content": "draft\n"})), openai_text("ok"))
    assert send(client, new_conversation(client), "draft notes") == "done"
    model.queue(openai_tool_call(("write_file", {"path": "notes.md", "content": "other\n"})))
    assert send(client, new_conversation(client), "overwrite notes") == "needs_approval"
    assert (ws / "notes.md").read_text() == "draft\n"


def test_a_file_that_appears_before_creation_is_never_overwritten(tmp_path):
    ws = Workspace(tmp_path)
    (tmp_path / "new.md").write_text("someone else's")
    with pytest.raises(FileChanged):
        ws.write_checked("new.md", "mine", mode="create")
    assert (tmp_path / "new.md").read_text() == "someone else's"


def test_the_check_and_the_write_use_the_same_open_file(tmp_path):
    ws = Workspace(tmp_path)
    _, fp = ws.write_checked("a.md", "one", mode="create")
    (tmp_path / "a.md").write_text("two")
    with pytest.raises(FileChanged):
        ws.write_checked("a.md", "three", mode="replace", expect=fp)
    assert (tmp_path / "a.md").read_text() == "two"


def test_appends_are_bounded_per_call_and_per_file(tmp_path):
    ws = Workspace(tmp_path)
    ctx = ToolContext(ws)
    execute(ctx, "write_file", {"path": "big.md", "content": "x" * (MAX_WRITE_BYTES - 10)})
    with pytest.raises(Exception, match="5 MB"):
        execute(ctx, "append_file", {"path": "big.md", "content": "y" * 20})
    with pytest.raises(Exception, match="too large for one call"):
        execute(ctx, "append_file", {"path": "big.md", "content": "y" * (MAX_APPEND_BYTES + 1)})
    assert os.path.getsize(tmp_path / "big.md") == MAX_WRITE_BYTES - 10
    with pytest.raises(Exception, match="does not exist"):
        execute(ctx, "append_file", {"path": "missing.md", "content": "y"})


def test_execute_without_a_record_still_refuses(tmp_path):
    (tmp_path / "a.md").write_text("user")
    ctx = ToolContext(Workspace(tmp_path))
    with pytest.raises(ApprovalRequired):
        execute(ctx, "append_file", {"path": "a.md", "content": "x"})
    assert (tmp_path / "a.md").read_text() == "user"


def test_records_come_only_from_the_worker_s_own_tool_results():
    record = {"path": "a.md", "dev": 1, "ino": 2, "size": 3, "sha256": "0" * 64}
    messages = [{"role": "user", "content": "x", "meta": {"worker_file": record}},
                {"role": "tool", "content": "Created a.md", "meta": {"worker_file": record}},
                {"role": "tool", "content": "Overwrote b.md", "meta": {"worker_file": {"path": "b.md", "released": True}}}]
    assert set(owned_files(messages)) == {"a.md"}
    assert owned_files(messages[:1]) == {}


def test_a_cut_off_tool_call_is_never_executed(worker):
    client, ws, model = worker
    model.queue(cut_off(openai_tool_call(("write_file", {"path": "report.md", "content": "# Report\n\nFirst half"}))),
                openai_text("I will write it in parts."))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "write a long report") == "done"
    assert not (ws / "report.md").exists()
    tool = [m for m in conversation(client, conv_id)["messages"] if m["role"] == "tool"][0]
    assert "output limit" in tool["content"] and "append_file" in tool["content"]
    # The model was told why, in the next request.
    assert "output limit" in model.requests[1]["body"]["messages"][-1]["content"]


def test_a_cut_off_reply_without_a_call_continues_the_run_with_a_labelled_note(worker):
    """Ollama returns a half-generated tool call as plain text: the run goes on (real L run)."""
    client, ws, model = worker
    model.queue(openai_tool_call(("write_file", {"path": "h.md", "content": "# Part 1\n"})),
                cut_off(openai_text('{"name": "append_file", "arguments": {"content": "## Part 2 ... (cut')),
                openai_tool_call(("append_file", {"path": "h.md", "content": "## Part 2\n"})),
                openai_text("Done."))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "write a long handbook") == "done"
    assert (ws / "h.md").read_text() == "# Part 1\n## Part 2\n"
    note = model.requests[2]["body"]["messages"][-1]
    assert note["role"] == "user" and note["content"].startswith("[Automatic note from Nature AI Worker, not from the user]")
    assert "smaller append_file calls" in note["content"]
    messages = conversation(client, conv_id)["messages"]
    assert [m["role"] for m in messages].count("user") == 1          # the note is not shown as the user's
    assert not approvals(client, conv_id)


def test_a_cut_off_reply_at_the_step_limit_ends_with_a_notice(client, fake_model):
    configure(client, endpoint=fake_model.url + "/v1", max_steps=2)
    fake_model.queue(cut_off(openai_text("part")), cut_off(openai_text("part again")))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "write it here") == "done"
    assert len(fake_model.requests) == 2                              # the 20-step style boundary holds
    last = conversation(client, conv_id)["messages"][-1]
    assert last["role"] == "notice" and "output limit" in last["content"]


def _cut(provider, response):
    status, body = response
    if provider == "anthropic":
        body["stop_reason"] = "max_tokens"
    elif provider == "gemini":
        body["candidates"][0]["finishReason"] = "MAX_TOKENS"
    else:
        body["done_reason"] = "length"
    return status, body


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "ollama"])
def test_every_adapter_reports_a_cut_off_reply(client, config, fake_model, monkeypatch, provider):
    from conftest import anthropic_response, gemini_response, ollama_response
    from nature_worker import providers
    monkeypatch.setattr(providers, "ANTHROPIC_BASE_URL", fake_model.url)
    monkeypatch.setattr(providers, "GEMINI_BASE_URL", fake_model.url + "/")
    if provider == "ollama":
        configure(client, provider="ollama", model="qwen3:8b", endpoint=fake_model.url, api_key=None)
    else:
        configure(client, provider=provider, model="m", api_key="sk-test-secret-123456")
    make = {"anthropic": anthropic_response, "gemini": gemini_response, "ollama": ollama_response}[provider]
    done = {"anthropic": anthropic_response(text="ok"), "gemini": gemini_response(text="ok"),
            "ollama": ollama_response(text="ok")}[provider]
    fake_model.queue(_cut(provider, make(("write_file", {"path": "r.md", "content": "half"}))), done)
    conv_id = new_conversation(client)
    assert send(client, conv_id, "write r.md") == "done"
    assert not (config.workspace_dir / "r.md").exists()
    tool = [m for m in conversation(client, conv_id)["messages"] if m["role"] == "tool"][0]
    assert "output limit" in tool["content"]
