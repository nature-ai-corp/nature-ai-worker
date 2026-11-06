# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""User-requested Stop: takes effect between steps. A model call or tool already running (a file
write included) finishes; nothing after it starts; finished work and approvals stay intact."""
import threading

from nature_worker.agent import STOPPED_CALL, Agent
from nature_worker.db import Database
from nature_worker.providers import ChatResult, ToolCall
from nature_worker.tools import ToolContext
from nature_worker.tools.workspace import Workspace
from nature_worker.work import summarize


class Model:
    """Replies from a script; ``hold`` blocks the next call until released (a slow model)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0
        self.entered, self.release = threading.Event(), threading.Event()
        self.hold = False

    def chat(self, system, messages, tools):
        self.calls += 1
        if self.hold:
            self.entered.set()
            assert self.release.wait(10)
        return self.replies.pop(0)


def calls(*specs):
    return ChatResult("", [ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(specs)])


def setup(tmp_path, model):
    db = Database(tmp_path / "w.db")
    ws = tmp_path / "ws"
    ws.mkdir()
    agent = Agent(db, lambda: model, lambda: ToolContext(Workspace(ws)), lambda: 20)
    return db, ws, agent, db.create_conversation()["id"]


def run_async(agent, conv_id, text):
    out = {}
    thread = threading.Thread(target=lambda: out.setdefault("status", agent.send(conv_id, text)))
    thread.start()
    return thread, out


def test_stop_during_a_model_call_runs_none_of_its_tool_calls(tmp_path):
    model = Model(calls(("write_file", {"path": "a.md", "content": "A"}), ("write_file", {"path": "b.md", "content": "B"})))
    model.hold = True
    db, ws, agent, conv = setup(tmp_path, model)
    thread, out = run_async(agent, conv, "write two files")
    assert model.entered.wait(10)
    assert agent.stop(conv) is True
    model.release.set()
    thread.join(10)
    assert out["status"] == "stopped" and model.calls == 1
    assert not (ws / "a.md").exists() and not (ws / "b.md").exists()
    messages = db.get_messages(conv)
    assert [m["content"] for m in messages if m["role"] == "tool"] == [STOPPED_CALL, STOPPED_CALL]
    assert messages[-1]["role"] == "notice" and messages[-1]["meta"]["error"] == "stopped"
    work = summarize(db.get_conversation(conv), messages, [], running=False, workspace=Workspace(ws))
    assert work["status"] == "stopped"


def test_a_running_file_write_finishes_and_the_next_step_does_not_start(tmp_path, monkeypatch):
    big = "x" * 2_000_000
    model = Model(calls(("write_file", {"path": "a.md", "content": big}), ("write_file", {"path": "b.md", "content": "B"})),
                  ChatResult("never asked", []))
    db, ws, agent, conv = setup(tmp_path, model)
    original = Agent._run_tool

    def first_tool_then_stop(self, *args, **kwargs):
        done = original(self, *args, **kwargs)
        self.stop(conv)  # the user clicks Stop while the first write runs
        return done
    monkeypatch.setattr(Agent, "_run_tool", first_tool_then_stop)
    assert agent.send(conv, "write files") == "stopped"
    assert (ws / "a.md").read_text() == big          # complete, never half-written
    assert not (ws / "b.md").exists() and model.calls == 1
    tools = [m for m in db.get_messages(conv) if m["role"] == "tool"]
    assert tools[0]["content"].startswith("Created a.md") and tools[1]["content"] == STOPPED_CALL
    # The finished write is still the Worker's file: the next run may revise it without approval.
    monkeypatch.setattr(Agent, "_run_tool", original)
    model.replies[:] = [calls(("append_file", {"path": "a.md", "content": "y"})), ChatResult("done", [])]
    assert agent.send(conv, "continue") == "done"
    assert (ws / "a.md").read_text() == big + "y"


def test_stop_is_only_for_the_run_it_was_asked_for(tmp_path):
    model = Model(ChatResult("hello", []), ChatResult("again", []))
    db, ws, agent, conv = setup(tmp_path, model)
    assert agent.stop(conv) is False                  # nothing running: nothing to stop
    assert agent.send(conv, "hi") == "done"
    assert agent.send(conv, "hi again") == "done"     # no stale stop carried over


def test_stop_never_touches_a_pending_approval(tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "plan.md").write_text("user")
    db = Database(tmp_path / "w.db")
    model = Model(calls(("write_file", {"path": "plan.md", "content": "changed"})), ChatResult("ok", []))
    agent = Agent(db, lambda: model, lambda: ToolContext(Workspace(tmp_path / "ws")), lambda: 20)
    conv = db.create_conversation()["id"]
    assert agent.send(conv, "change plan") == "needs_approval"
    assert agent.stop(conv) is False                  # paused, not running
    approval = db.pending_approvals(conv)[0]
    assert agent.resolve_approval(approval, False) == "done"
    assert (tmp_path / "ws" / "plan.md").read_text() == "user"


def test_stop_endpoint(client):
    conv = client.post("/api/conversations").json()["id"]
    assert client.post(f"/api/conversations/{conv}/stop").json() == {"stopping": False}
    assert client.post("/api/conversations/missing/stop").status_code == 404
