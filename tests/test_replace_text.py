# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""replace_text: one exact, unique match replaced atomically in the file's own encoding. A file the
Worker created in this conversation may be corrected while it is still exactly what it wrote; every
other existing file needs approval, and links, other paths and swapped files are refused."""
import os

import pytest

from conftest import configure, conversation, new_conversation, openai_text, openai_tool_call, send
from nature_worker.tools import ApprovalRequired, ToolContext, ToolError, execute, owned_files
from nature_worker.tools.workspace import FileChanged, Workspace


@pytest.fixture
def worker(client, config, fake_model):
    configure(client, endpoint=fake_model.url + "/v1")
    return client, config.workspace_dir, fake_model


def pending(client, conv_id):
    return [a for a in conversation(client, conv_id)["approvals"] if a["status"] == "pending"]


def _ctx(tmp_path, name="r.md", data=b"# R\n\nNorth: 170\nSouth: 90\n"):
    """A context in which the Worker created ``name`` with ``data``."""
    ctx = ToolContext(Workspace(tmp_path))
    execute(ctx, "write_file", {"path": name, "content": "x"})
    (tmp_path / name).write_bytes(data)          # same inode, so refresh the record to match
    ctx.owned[name] = ctx.workspace.fingerprint(name)
    return ctx


def test_the_worker_corrects_its_own_report_without_approval(worker):
    client, ws, model = worker
    model.queue(
        openai_tool_call(("write_file", {"path": "q3.md", "content": "# Q3\n\nNorth: 170\nSouth: 90\nTotal: 250\n"})),
        openai_tool_call(("replace_text", {"path": "q3.md", "old_text": "Total: 250", "new_text": "Total: 260"})),
        openai_text("Corrected the total."),
    )
    conv_id = new_conversation(client)
    assert send(client, conv_id, "write the report and check it") == "done"
    assert (ws / "q3.md").read_text() == "# Q3\n\nNorth: 170\nSouth: 90\nTotal: 260\n"
    assert pending(client, conv_id) == []
    conv = conversation(client, conv_id)
    result = [m for m in conv["messages"] if m["role"] == "tool"][-1]
    assert result["content"] == "Replaced 1 occurrence in q3.md at line 5; it now has 38 bytes."
    assert result["meta"]["worker_file"]["size"] == 38          # still the Worker's file, for later edits
    assert conv["work"]["deliverables"][0]["path"] == "q3.md"
    # Later messages may correct it again; the original call and arguments stay in the history.
    model.queue(openai_tool_call(("replace_text", {"path": "q3.md", "old_text": "South: 90", "new_text": "South: 100"})),
                openai_text("ok"))
    assert send(client, conv_id, "fix south") == "done"
    assert "South: 100" in (ws / "q3.md").read_text()
    calls = [c for m in conversation(client, conv_id)["messages"] for c in (m["tool_calls"] or [])]
    assert calls[1]["arguments"] == {"path": "q3.md", "old_text": "Total: 250", "new_text": "Total: 260"}


def test_a_users_file_needs_approval_and_a_denial_leaves_it_unchanged(worker):
    client, ws, model = worker
    (ws / "plan.md").write_text("the user's plan: 3 steps\n")
    model.queue(openai_tool_call(("replace_text", {"path": "plan.md", "old_text": "3 steps", "new_text": "4 steps"})))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "fix plan.md") == "needs_approval"
    assert (ws / "plan.md").read_text() == "the user's plan: 3 steps\n"
    [a] = pending(client, conv_id)
    assert a["reason"] == "Edit existing file 'plan.md'" and a["arguments"]["new_text"] == "4 steps"
    model.queue(openai_text("Left it as it is."))
    r = client.post(f"/api/approvals/{a['id']}", json={"approve": False})
    assert r.json()["status"] == "done"
    assert (ws / "plan.md").read_text() == "the user's plan: 3 steps\n"


def test_an_approved_edit_changes_only_that_text_and_the_file_stays_the_users(worker):
    client, ws, model = worker
    (ws / "plan.md").write_text("the user's plan: 3 steps\nkeep this\n")
    model.queue(openai_tool_call(("replace_text", {"path": "plan.md", "old_text": "3 steps", "new_text": "4 steps"})))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "fix plan.md") == "needs_approval"
    model.queue(openai_tool_call(("replace_text", {"path": "plan.md", "old_text": "keep", "new_text": "drop"})))
    r = client.post(f"/api/approvals/{pending(client, conv_id)[0]['id']}", json={"approve": True})
    assert r.json()["status"] == "needs_approval"           # the next edit asks again
    assert (ws / "plan.md").read_text() == "the user's plan: 4 steps\nkeep this\n"


@pytest.mark.parametrize("old,count", [("missing", 0), ("o", 2), ("North: 170\nsouth", 0)])
def test_zero_or_several_matches_are_refused_and_nothing_changes(tmp_path, old, count):
    ctx = _ctx(tmp_path, data=b"# R\n\nNorth: 170\nSouth: 90\n")
    with pytest.raises(ToolError, match=f"found {count} times"):
        execute(ctx, "replace_text", {"path": "r.md", "old_text": old, "new_text": "x"})
    assert (tmp_path / "r.md").read_bytes() == b"# R\n\nNorth: 170\nSouth: 90\n"
    assert os.listdir(tmp_path) == ["r.md"]                  # no temporary file left behind


@pytest.mark.parametrize("args,message", [
    ({"path": "r.md", "old_text": "", "new_text": "x"}, "must not be empty"),
    ({"path": "r.md", "old_text": "North", "new_text": None}, "must be strings"),
    ({"path": "none.md", "old_text": "a", "new_text": "b"}, "File not found"),
    ({"path": "../r.md", "old_text": "a", "new_text": "b"}, "outside the workspace"),
    ({"path": "/etc/passwd", "old_text": "root", "new_text": "x"}, "absolute path"),
    ({"path": "report.docx", "old_text": "a", "new_text": "b"}, "text files only"),
])
def test_invalid_calls_are_refused(tmp_path, args, message):
    ctx = _ctx(tmp_path)
    with pytest.raises(ToolError, match=message):
        execute(ctx, "replace_text", args)


def _encoded(text, codec, bom=b""):
    return bom + text.encode(codec)


@pytest.mark.parametrize("codec,bom", [
    ("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"), ("cp1252", b""),
])
def test_the_files_encoding_byte_order_mark_and_line_endings_are_kept(tmp_path, codec, bom):
    ctx = _ctx(tmp_path, data=_encoded("Price: 10 €\r\nEnd\r\n", codec, bom))
    execute(ctx, "replace_text", {"path": "r.md", "old_text": "10 €", "new_text": "12 €"})
    assert (tmp_path / "r.md").read_bytes() == _encoded("Price: 12 €\r\nEnd\r\n", codec, bom)


def test_text_the_encoding_cannot_hold_is_refused(tmp_path):
    ctx = _ctx(tmp_path, data="Caf\xe9\n".encode("cp1252"))
    with pytest.raises(ToolError, match="cannot be saved in this file's encoding"):
        execute(ctx, "replace_text", {"path": "r.md", "old_text": "Caf", "new_text": "日本"})
    assert (tmp_path / "r.md").read_bytes() == b"Caf\xe9\n"


def test_windows_line_endings_are_named_when_nothing_matches(tmp_path):
    ctx = _ctx(tmp_path, data=b"a\r\nb\r\n")
    with pytest.raises(ToolError, match=r"Windows line endings"):
        execute(ctx, "replace_text", {"path": "r.md", "old_text": "a\nb", "new_text": "c"})


def test_the_replacement_is_atomic_and_keeps_the_file_mode(tmp_path):
    ctx = _ctx(tmp_path)
    os.chmod(tmp_path / "r.md", 0o600)
    ctx.owned["r.md"] = ctx.workspace.fingerprint("r.md")
    before = (tmp_path / "r.md").stat().st_ino
    out = execute(ctx, "replace_text", {"path": "r.md", "old_text": "South: 90", "new_text": "South: 95"})
    st = (tmp_path / "r.md").stat()
    assert st.st_ino != before                                # a new file renamed into place
    assert st.st_mode & 0o777 == 0o600
    assert out.meta["worker_file"] == {"path": "r.md", **ctx.workspace.fingerprint("r.md").to_dict()}
    assert owned_files([{"role": "tool", "meta": out.meta}]) == {"r.md": ctx.workspace.fingerprint("r.md")}


def test_a_worker_file_changed_by_the_user_needs_approval(tmp_path):
    ctx = _ctx(tmp_path)
    (tmp_path / "r.md").write_text("# R\n\nNorth: 170\nSouth: 90\nuser note\n")
    with pytest.raises(ApprovalRequired, match="Edit existing file 'r.md'"):
        execute(ctx, "replace_text", {"path": "r.md", "old_text": "South: 90", "new_text": "South: 95"})
    assert (tmp_path / "r.md").read_text().endswith("South: 90\nuser note\n")


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
def test_symlinks_are_never_followed(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("password: a\n")
    ctx = ToolContext(Workspace(ws))
    os.symlink(secret, ws / "out.md")                        # points outside the workspace
    with pytest.raises(ToolError, match="outside the workspace"):
        execute(ctx, "replace_text", {"path": "out.md", "old_text": "a", "new_text": "b"}, approved=True)
    (ws / "inner.md").write_text("password: a\n")
    os.symlink(ws / "inner.md", ws / "link.md")              # a link inside: it is the user's file, so it asks
    with pytest.raises(ApprovalRequired, match="Edit existing file 'link.md'"):
        execute(ctx, "replace_text", {"path": "link.md", "old_text": "a\n", "new_text": "b\n"})
    assert secret.read_text() == "password: a\n" and (ws / "inner.md").read_text() == "password: a\n"


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
def test_a_worker_file_swapped_for_a_link_to_a_user_file_needs_approval(tmp_path):
    ctx = _ctx(tmp_path)
    (tmp_path / "user.md").write_text("# R\n\nNorth: 170\nSouth: 90\n")   # same content, other file
    os.unlink(tmp_path / "r.md")
    os.symlink(tmp_path / "user.md", tmp_path / "r.md")
    with pytest.raises(ApprovalRequired):
        execute(ctx, "replace_text", {"path": "r.md", "old_text": "South: 90", "new_text": "South: 95"})
    assert (tmp_path / "user.md").read_text().endswith("South: 90\n")


def test_a_hard_linked_file_is_refused(tmp_path):
    ctx = _ctx(tmp_path)
    os.link(tmp_path / "r.md", tmp_path / "other.md")
    with pytest.raises(ToolError, match="hard links"):
        execute(ctx, "replace_text", {"path": "r.md", "old_text": "South: 90", "new_text": "South: 95"}, approved=True)
    assert (tmp_path / "other.md").read_text().endswith("South: 90\n")


def test_a_change_between_reading_and_renaming_aborts_the_edit(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    ws = ctx.workspace
    real_fsync = os.fsync

    def fsync_then_user_edits(fd):
        real_fsync(fd)
        with open(tmp_path / "r.md", "a") as f:              # the user saves the file meanwhile
            f.write("user line\n")

    monkeypatch.setattr(os, "fsync", fsync_then_user_edits)
    with pytest.raises(FileChanged, match="changed while it was being edited"):
        ws.replace_checked("r.md", "South: 90", "South: 95")
    assert (tmp_path / "r.md").read_text() == "# R\n\nNorth: 170\nSouth: 90\nuser line\n"
    assert sorted(os.listdir(tmp_path)) == ["r.md"]


def test_the_result_is_bounded(tmp_path):
    ctx = _ctx(tmp_path, data=b"a" * 10 + b"\n")
    with pytest.raises(ToolError, match="5 MB"):
        execute(ctx, "replace_text", {"path": "r.md", "old_text": "aaaaaaaaaa", "new_text": "b" * 5_000_001})
    assert (tmp_path / "r.md").read_bytes() == b"a" * 10 + b"\n"
