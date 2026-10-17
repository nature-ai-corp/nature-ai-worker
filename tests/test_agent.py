"""Agent execution through the real application (acceptance D, multi-step runs)."""

from conftest import configure, conversation, new_conversation, openai_text, openai_tool_call, send


def test_agent_reads_workspace_and_writes_report(client, config, fake_model):
    (config.workspace_dir / "sales.csv").write_text("region,amount\nNorth,120\nSouth,80\nNorth,50\n")
    (config.workspace_dir / "notes.txt").write_text("Q3 focus: grow the South region.\n")
    configure(client, endpoint=fake_model.url + "/v1")

    report = "# Sales report\n\nTotal sales: 250\n\nNote: Q3 focus is the South region.\n"
    fake_model.queue(
        openai_tool_call(("list_files", {})),
        openai_tool_call(("read_file", {"path": "notes.txt"}), ("table_summary", {"path": "sales.csv"})),
        openai_tool_call(("calculate", {"expression": "120+80+50"})),
        openai_tool_call(("write_file", {"path": "report.md", "content": report})),
        openai_text("I wrote report.md with total sales of 250."),
    )
    conv_id = new_conversation(client)
    assert send(client, conv_id, "Summarize my workspace data into report.md") == "done"

    # The file was really written to the workspace.
    assert (config.workspace_dir / "report.md").read_text() == report

    conv = conversation(client, conv_id)
    roles = [m["role"] for m in conv["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "tool", "assistant", "tool", "assistant", "tool", "assistant"]
    tool_outputs = {m["tool_name"]: m["content"] for m in conv["messages"] if m["role"] == "tool"}
    assert "sales.csv" in tool_outputs["list_files"]
    assert "Q3 focus" in tool_outputs["read_file"]
    assert "sum=250" in tool_outputs["table_summary"]
    assert "= 250" in tool_outputs["calculate"]
    assert tool_outputs["write_file"].startswith("Created report.md")
    assert conv["messages"][-1]["content"] == "I wrote report.md with total sales of 250."
    assert conv["title"].startswith("Summarize my workspace")

    # Tool results were sent back to the model in the OpenAI format with matching ids.
    second = fake_model.requests[1]["body"]
    assert second["messages"][0]["role"] == "system"
    tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["tool_call_id"] == "call_list_files_0"
    assert {t["function"]["name"] for t in second["tools"]} >= {"list_files", "read_file", "write_file"}
    assert "web_fetch" not in {t["function"]["name"] for t in second["tools"]}  # disabled by default

    # The report shows up in the workspace file listing and can be previewed.
    files = client.get("/api/files").json()["files"]
    assert any(f["path"] == "report.md" for f in files)
    assert client.get("/api/files/content", params={"path": "report.md"}).json()["content"] == report


def test_step_limit_bounds_the_run(client, fake_model):
    configure(client, endpoint=fake_model.url + "/v1", max_steps=3)
    fake_model.queue(*[openai_tool_call(("list_files", {})) for _ in range(5)])
    conv_id = new_conversation(client)
    assert send(client, conv_id, "loop forever") == "limit"
    conv = conversation(client, conv_id)
    assert len(fake_model.requests) == 3
    assert conv["messages"][-1]["role"] == "notice"
    assert "Stopped after 3 steps" in conv["messages"][-1]["content"]


def test_tool_errors_are_reported_to_the_model(client, fake_model):
    configure(client, endpoint=fake_model.url + "/v1")
    fake_model.queue(
        openai_tool_call(("read_file", {"path": "missing.txt"}), ("no_such_tool", {})),
        openai_text("The file does not exist."),
    )
    conv_id = new_conversation(client)
    assert send(client, conv_id, "read missing.txt") == "done"
    tools = [m for m in conversation(client, conv_id)["messages"] if m["role"] == "tool"]
    assert tools[0]["content"] == "Error: File not found: missing.txt"
    assert "Unknown or disabled tool" in tools[1]["content"]
    assert all(t["meta"]["error"] for t in tools)


def test_invalid_tool_arguments_are_reported(client, fake_model):
    configure(client, endpoint=fake_model.url + "/v1")
    bad = openai_tool_call(("read_file", {}))
    bad[1]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
    fake_model.queue(bad, openai_text("Sorry."))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "go") == "done"
    tool = [m for m in conversation(client, conv_id)["messages"] if m["role"] == "tool"][0]
    assert "not a valid JSON object" in tool["content"]


def test_unconfigured_app_reports_setup_needed(client):
    conv_id = new_conversation(client)
    assert send(client, conv_id, "hello") == "error"
    notice = conversation(client, conv_id)["messages"][-1]
    assert notice["role"] == "notice"
    assert "Open Settings" in notice["content"]


def test_reused_tool_call_ids_are_made_unique(client, config, fake_model):
    # The fake server returns id "call_write_file_0" on both turns, like some OpenAI-compatible servers.
    (config.workspace_dir / "a.txt").write_text("v1")
    configure(client, endpoint=fake_model.url + "/v1")
    fake_model.queue(openai_tool_call(("write_file", {"path": "b.txt", "content": "x"})), openai_text("one"))
    conv_id = new_conversation(client)
    send(client, conv_id, "first")
    fake_model.queue(openai_tool_call(("write_file", {"path": "a.txt", "content": "v2"})), openai_text("two"))
    assert send(client, conv_id, "second") == "needs_approval"
    conv = conversation(client, conv_id)
    ids = [c["id"] for m in conv["messages"] if m["role"] == "assistant" for c in (m["tool_calls"] or [])]
    assert len(ids) == len(set(ids)) == 2
    assert conv["approvals"][0]["tool_call_id"] == ids[1]
