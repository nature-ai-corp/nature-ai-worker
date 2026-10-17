"""Workspace confinement (acceptance E), approvals (F) and credential handling."""

import logging
import os

import pytest

from conftest import configure, conversation, new_conversation, openai_text, openai_tool_call, send
from nature_worker.credentials import CredentialStore, RedactSecretsFilter
from nature_worker.tools import ApprovalRequired, ToolContext, ToolError, execute
from nature_worker.tools.workspace import Workspace


@pytest.fixture
def sandbox(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP-SECRET")
    (ws / "ok.txt").write_text("fine")
    os.symlink(outside / "secret.txt", ws / "link_to_secret.txt")
    os.symlink(outside, ws / "link_dir")
    os.symlink(ws / "ok.txt", ws / "inner_link.txt")
    return Workspace(ws), outside


# -- E: workspace escape --------------------------------------------------------

@pytest.mark.parametrize("path", [
    "../outside/secret.txt", "../../etc/passwd", "/etc/passwd", "~/.ssh/id_rsa",
    "sub/../../outside/secret.txt", "link_to_secret.txt", "link_dir/secret.txt", "link_dir",
    "C:/Windows/win.ini", "a\x00b",
])
def test_escape_attempts_are_blocked(sandbox, path):
    ws, _ = sandbox
    with pytest.raises(ToolError):
        ws.read_text(path)


@pytest.mark.parametrize("path", ["../evil.txt", "link_dir/evil.txt", "link_to_secret.txt", "/tmp/evil.txt"])
def test_writes_outside_workspace_are_blocked(sandbox, path):
    ws, outside = sandbox
    with pytest.raises(ToolError):
        ws.write_text(path, "pwned")
    assert (outside / "secret.txt").read_text() == "TOP-SECRET"
    assert not (outside / "evil.txt").exists()


def test_symlinks_inside_workspace_still_work(sandbox):
    ws, _ = sandbox
    assert ws.read_text("inner_link.txt") == "fine"


def test_listing_hides_escaping_symlinks(sandbox):
    ws, _ = sandbox
    paths = {e["path"] for e in ws.list(".")}
    assert "ok.txt" in paths and "inner_link.txt" in paths
    assert "link_to_secret.txt" not in paths and "link_dir" not in paths
    assert not any("secret" in p and p != "link_to_secret.txt" for p in paths)


def test_agent_escape_attempt_through_app(client, config, fake_model):
    secret = config.workspace_dir.parent / "secret.txt"
    secret.write_text("TOP-SECRET")
    configure(client, endpoint=fake_model.url + "/v1")
    fake_model.queue(
        openai_tool_call(("read_file", {"path": "../secret.txt"}), ("read_file", {"path": str(secret)}),
                         ("write_file", {"path": "../evil.txt", "content": "x"}),
                         ("list_files", {"path": ".."})),
        openai_text("I cannot access files outside the workspace."),
    )
    conv_id = new_conversation(client)
    assert send(client, conv_id, "read ../secret.txt") == "done"
    msgs = conversation(client, conv_id)["messages"]
    tool_results = [m["content"] for m in msgs if m["role"] == "tool"]
    assert all(r.startswith("Error:") for r in tool_results), tool_results
    assert not any("TOP-SECRET" in m["content"] for m in msgs)
    assert "TOP-SECRET" not in str(fake_model.requests[-1]["body"])
    assert not (config.workspace_dir.parent / "evil.txt").exists()


def test_file_api_rejects_escapes(client, config):
    (config.workspace_dir.parent / "secret.txt").write_text("TOP-SECRET")
    for path in ["../secret.txt", "/etc/passwd"]:
        assert client.get("/api/files/content", params={"path": path}).status_code == 400
        assert client.get("/api/files/download", params={"path": path}).status_code == 400
        assert client.put("/api/files", params={"path": path}, content=b"x").status_code == 400


# -- F: approvals are enforced by the execution layer ---------------------------------

def test_execute_refuses_sensitive_calls_without_approval(tmp_path):
    ws = Workspace(tmp_path)
    (tmp_path / "a.txt").write_text("original")
    ctx = ToolContext(ws, web_fetch_enabled=True)
    with pytest.raises(ApprovalRequired):
        execute(ctx, "write_file", {"path": "a.txt", "content": "changed"})
    with pytest.raises(ApprovalRequired):
        execute(ctx, "delete_file", {"path": "a.txt"})
    with pytest.raises(ApprovalRequired):
        execute(ctx, "web_fetch", {"url": "https://example.com"})
    assert (tmp_path / "a.txt").read_text() == "original"
    # Creating a new file is not sensitive.
    assert execute(ctx, "write_file", {"path": "new.txt", "content": "x"}).startswith("Created")
    # web_fetch is unavailable unless enabled in settings, even with approval.
    with pytest.raises(ToolError):
        execute(ToolContext(ws, web_fetch_enabled=False), "web_fetch", {"url": "https://example.com"}, approved=True)


def _overwrite_flow(client, config, fake_model):
    (config.workspace_dir / "plan.md").write_text("original")
    configure(client, endpoint=fake_model.url + "/v1")
    fake_model.queue(
        openai_tool_call(("write_file", {"path": "plan.md", "content": "replaced"})),
        openai_text("Done."),
    )
    conv_id = new_conversation(client)
    assert send(client, conv_id, "rewrite plan.md") == "needs_approval"
    conv = conversation(client, conv_id)
    assert (config.workspace_dir / "plan.md").read_text() == "original"
    assert len(conv["approvals"]) == 1
    assert conv["approvals"][0]["reason"] == "Overwrite existing file 'plan.md'"
    assert len(fake_model.requests) == 1  # the run is paused, the model was not called again
    return conv_id, conv["approvals"][0]


def test_overwrite_waits_for_approval_then_runs(client, config, fake_model):
    conv_id, approval = _overwrite_flow(client, config, fake_model)
    r = client.post(f"/api/approvals/{approval['id']}", json={"approve": True})
    assert r.json()["status"] == "done"
    assert (config.workspace_dir / "plan.md").read_text() == "replaced"
    # An approval can only be used once.
    assert client.post(f"/api/approvals/{approval['id']}", json={"approve": True}).status_code == 409


def test_denied_operation_is_not_performed(client, config, fake_model):
    conv_id, approval = _overwrite_flow(client, config, fake_model)
    assert client.post(f"/api/approvals/{approval['id']}", json={"approve": False}).json()["status"] == "done"
    assert (config.workspace_dir / "plan.md").read_text() == "original"
    tool = [m for m in conversation(client, conv_id)["messages"] if m["role"] == "tool"][0]
    assert "denied" in tool["content"]
    # The model was told about the denial.
    assert "denied" in fake_model.requests[-1]["body"]["messages"][-1]["content"]


def test_new_message_cancels_pending_approval(client, config, fake_model):
    conv_id, approval = _overwrite_flow(client, config, fake_model)
    fake_model.responses.clear()
    fake_model.queue(openai_text("OK, I left it alone."))
    assert send(client, conv_id, "never mind") == "done"
    assert (config.workspace_dir / "plan.md").read_text() == "original"
    assert conversation(client, conv_id)["approvals"] == []
    assert client.post(f"/api/approvals/{approval['id']}", json={"approve": True}).status_code == 409
    assert (config.workspace_dir / "plan.md").read_text() == "original"


def test_delete_requires_approval(client, config, fake_model):
    (config.workspace_dir / "keep.txt").write_text("data")
    configure(client, endpoint=fake_model.url + "/v1")
    fake_model.queue(openai_tool_call(("delete_file", {"path": "keep.txt"})), openai_text("Deleted."))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "delete keep.txt") == "needs_approval"
    assert (config.workspace_dir / "keep.txt").exists()
    approval = conversation(client, conv_id)["approvals"][0]
    client.post(f"/api/approvals/{approval['id']}", json={"approve": True})
    assert not (config.workspace_dir / "keep.txt").exists()


def test_cross_origin_requests_are_blocked(client):
    r = client.post("/api/conversations", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/settings", headers={"Host": "evil.example"}).status_code == 400


# -- credentials -------------------------------------------------------------------

def test_api_key_never_stored_in_plaintext_or_returned(client, config, fake_model, caplog):
    key = "sk-test-SUPER-SECRET-KEY-987654"
    view = configure(client, endpoint=fake_model.url + "/v1", api_key=key)
    assert view["api_key_source"] == "stored"
    assert key not in str(view)
    assert key not in client.get("/api/settings").text
    for f in config.data_dir.iterdir():
        assert key.encode() not in f.read_bytes(), f"key found in plaintext in {f.name}"
    assert (config.data_dir / "credentials.enc").stat().st_mode & 0o077 == 0
    assert (config.data_dir / "master.key").stat().st_mode & 0o077 == 0

    # The key is sent to the provider but not stored in the conversation.
    fake_model.queue(openai_text("hi"))
    conv_id = new_conversation(client)
    send(client, conv_id, "hello")
    assert fake_model.requests[-1]["headers"]["authorization"] == f"Bearer {key}"
    assert key not in str(conversation(client, conv_id))
    assert key.encode() not in (config.data_dir / "worker.db").read_bytes()

    # Saving settings without a key keeps the stored key.
    configure(client, endpoint=fake_model.url + "/v1", api_key=None)
    from nature_worker.providers import normalize_endpoint

    store = CredentialStore(config.data_dir)
    assert store.get("openai_compatible", normalize_endpoint(fake_model.url + "/v1")) == (key, "stored")


def test_environment_key_takes_precedence(config, monkeypatch):
    store = CredentialStore(config.data_dir)
    store.set("openai", "stored-key-123456")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key-123456")
    assert store.get("openai") == ("env-key-123456", "env")


def test_wrong_master_key_does_not_leak(config, monkeypatch):
    monkeypatch.setenv("NATURE_WORKER_SECRET_KEY", "passphrase-one")
    CredentialStore(config.data_dir).set("openai", "sk-abcdef123456")
    monkeypatch.setenv("NATURE_WORKER_SECRET_KEY", "passphrase-two")
    assert CredentialStore(config.data_dir).get("openai") == (None, "none")


def test_log_redaction(config):
    store = CredentialStore(config.data_dir)
    store.set("anthropic", "sk-ant-LEAKY-KEY-123456")
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "request failed with key %s", ("sk-ant-LEAKY-KEY-123456",), None)
    RedactSecretsFilter(store).filter(record)
    assert record.getMessage() == "request failed with key [REDACTED]"


def test_log_redaction_covers_tracebacks(config):
    import io

    store = CredentialStore(config.data_dir)
    store.set("openai", "sk-TRACEBACK-KEY-123456")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(RedactSecretsFilter(store))
    log = logging.getLogger("redaction-test")
    log.addHandler(handler)
    log.propagate = False
    try:
        raise RuntimeError("failed with sk-TRACEBACK-KEY-123456")
    except RuntimeError:
        log.exception("provider call failed")
    log.removeHandler(handler)
    assert "sk-TRACEBACK-KEY-123456" not in buf.getvalue()
    assert "RuntimeError: failed with [REDACTED]" in buf.getvalue()


def test_dangling_symlinks_do_not_break_listing(client, config, tmp_path):
    os.symlink(config.workspace_dir / "gone.txt", config.workspace_dir / "dangling")
    os.symlink(tmp_path / "nowhere", config.workspace_dir / "dangling_out")
    (config.workspace_dir / "real.txt").write_text("x")
    r = client.get("/api/files")
    assert r.status_code == 200
    assert [f["path"] for f in r.json()["files"]] == ["real.txt"]
