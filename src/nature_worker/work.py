"""Work view: a read-only summary of one conversation for the "My Work" interface.

Everything here is derived from records that already exist: messages, tool
results, approvals and the workspace files. Nothing is stored, and nothing is
inferred that the records do not show. In particular:

- The status describes how the agent's last run ended, not whether the user's
  goal was met. "finished" means the agent ended its run with a reply.
- A deliverable is a file this work created or overwrote with a successful
  write_file call. Files it only read are inputs. A deliverable that is no
  longer in the workspace, or was changed afterwards, is reported as such.
"""

from __future__ import annotations

from typing import Any

from .tools import ToolError
from .tools.workspace import Workspace

INPUT_TOOLS = ("read_file", "table_summary")

STATUS_LABELS = {
    "not_started": "Not started",
    "working": "Working",
    "needs_approval": "Needs your approval",
    "finished": "Finished",
    "stopped": "Stopped",
    "failed": "Failed",
    "interrupted": "Interrupted",
}


def _status(messages: list[dict], pending: list[dict], running: bool) -> tuple[str, str]:
    """Return (status, current update text) from the latest records."""
    if running:
        return "working", ""
    if pending:
        return "needs_approval", pending[0]["reason"]
    if not messages:
        return "not_started", ""
    last = messages[-1]
    if last["role"] == "notice":
        if (last["meta"] or {}).get("error") == "step_limit":
            return "stopped", last["content"]
        return "failed", last["content"]
    if last["role"] == "assistant" and not last["tool_calls"]:
        return "finished", last["content"]
    # The last record is the user's message, a tool result, or a tool call without a
    # result, and the agent is not running: the run ended without a reply.
    return "interrupted", (
        "The agent stopped before replying, for example because the app was closed or restarted. "
        "Nothing more will happen on its own. Send a message such as \"continue\" to start a new run."
    )


def _normalize(workspace: Workspace | None, path: str) -> str:
    """The path as the file list shows it, so "./report.md" and "report.md" are one file."""
    if workspace is None:
        return path
    try:
        return workspace.display(workspace.resolve(path))
    except (ToolError, ValueError):
        return path


def _file_facts(workspace: Workspace | None, path: str) -> dict | None:
    """Size and modification time if the file is in the workspace now, else None."""
    if workspace is None:
        return None
    try:
        full = workspace.resolve(path)
        if not full.is_file():
            return None
        st = full.stat()
    except (ToolError, OSError):
        return None
    return {"size": st.st_size, "modified_at": st.st_mtime}


def summarize(conv: dict, messages: list[dict], approvals: list[dict], running: bool,
              workspace: Workspace | None = None) -> dict[str, Any]:
    """Build the work summary. ``approvals`` are all approvals of the conversation (any status)."""
    pending = [a for a in approvals if a["status"] == "pending"]
    status, update = _status(messages, pending, running)

    calls = {c["id"]: c for m in messages if m["role"] == "assistant" for c in (m["tool_calls"] or [])}
    results = [m for m in messages if m["role"] == "tool"]
    failed = sum(1 for r in results if (r["meta"] or {}).get("error"))
    denied = sum(1 for r in results if (r["meta"] or {}).get("denied"))

    deliverables: dict[str, dict] = {}
    inputs: dict[str, dict] = {}
    for r in results:
        meta = r["meta"] or {}
        call = calls.get(r["tool_call_id"])
        if meta or not call or not isinstance((call.get("arguments") or {}).get("path"), str):
            continue  # only successful calls with a path count
        path = _normalize(workspace, call["arguments"]["path"])
        if r["tool_name"] == "write_file":
            deliverables[path] = {"written_at": r["created_at"], "action": r["content"].split(" ", 1)[0]}
        elif r["tool_name"] == "delete_file":
            deliverables.pop(path, None)
        elif r["tool_name"] in INPUT_TOOLS:
            inputs.setdefault(path, {"read_at": r["created_at"]})

    def files(entries: dict[str, dict], kind: str) -> list[dict]:
        out = []
        for path, info in entries.items():
            facts = _file_facts(workspace, path)
            item = {"path": path, "available": facts is not None, **info}
            if facts:
                item["size"] = facts["size"]
                if kind == "deliverable":
                    # Written again later (by the user, another work or the agent elsewhere).
                    item["changed_since"] = facts["modified_at"] > info["written_at"] + 1
            out.append(item)
        return out

    delivered = files(deliverables, "deliverable")
    used = [i for i in files(inputs, "input") if i["path"] not in deliverables]

    first_user = next((m for m in messages if m["role"] == "user"), None)
    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "update": update,
        "instruction": first_user["content"] if first_user else "",
        "facts": {
            "created_at": conv["created_at"],
            "updated_at": conv["updated_at"],
            "agent_steps": sum(1 for m in messages if m["role"] == "assistant"),
            "tool_calls": len(calls),
            "tool_failures": failed,
            "denied": denied,
            "approvals_approved": sum(1 for a in approvals if a["status"] == "approved"),
            "approvals_denied": sum(1 for a in approvals if a["status"] == "denied"),
            "approvals_pending": len(pending),
        },
        "deliverables": delivered,
        "inputs": used,
    }


__all__ = ["summarize", "STATUS_LABELS"]
