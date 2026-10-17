"""Built-in tools and the execution layer that enforces approval.

The approval policy is enforced here, not in the model prompt:
``execute()`` refuses to run a tool that needs approval unless the caller
passes ``approved=True``, which only the approval endpoint does after the
user clicks Approve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import data, web
from .workspace import ToolError, Workspace
from .workspace import delete_file, list_files, read_file, write_file


@dataclass
class ToolContext:
    workspace: Workspace
    web_fetch_enabled: bool = False


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[Workspace, dict], str]
    # Returns a human-readable reason when the call needs user approval, else None.
    approval: Callable[[ToolContext, dict], str | None] = lambda ctx, args: None

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


def _write_approval(ctx: ToolContext, args: dict) -> str | None:
    path = args.get("path")
    if isinstance(path, str) and path.strip():
        try:
            if ctx.workspace.exists(path):
                return f"Overwrite existing file '{path}'"
        except ToolError:
            return None  # invalid path: let execution report the error
    return None


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "list_files",
            "List files and folders in the workspace. Paths are relative to the workspace root.",
            _obj(
                {
                    "path": {"type": "string", "description": "Folder to list, relative to the workspace. Default '.'"},
                    "recursive": {"type": "boolean", "description": "Include subfolders. Default true."},
                },
                [],
            ),
            list_files,
        ),
        Tool(
            "read_file",
            "Read a UTF-8 text file from the workspace. Long files are returned in chunks.",
            _obj(
                {
                    "path": {"type": "string", "description": "File path relative to the workspace."},
                    "offset": {"type": "integer", "description": "Character offset to start from (for long files)."},
                },
                ["path"],
            ),
            read_file,
        ),
        Tool(
            "write_file",
            "Create or replace a text file in the workspace. Parent folders are created automatically. "
            "Replacing an existing file requires the user's approval.",
            _obj(
                {
                    "path": {"type": "string", "description": "File path relative to the workspace."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                ["path", "content"],
            ),
            write_file,
            _write_approval,
        ),
        Tool(
            "delete_file",
            "Delete a file from the workspace. Always requires the user's approval.",
            _obj({"path": {"type": "string", "description": "File path relative to the workspace."}}, ["path"]),
            delete_file,
            lambda ctx, args: f"Delete file '{args.get('path')}'",
        ),
        Tool(
            "search_text",
            "Search text files in the workspace for a word, phrase or regular expression (case-insensitive).",
            _obj(
                {
                    "query": {"type": "string", "description": "Text or regular expression to find."},
                    "path": {"type": "string", "description": "Folder to search. Default '.'"},
                    "regex": {"type": "boolean", "description": "Treat query as a regular expression."},
                },
                ["query"],
            ),
            data.search_text,
        ),
        Tool(
            "table_summary",
            "Summarize a CSV, TSV or JSON table: row count, columns, and per-column statistics "
            "(sum, min, max, mean, median for numbers; top values for text).",
            _obj({"path": {"type": "string", "description": "Table file path relative to the workspace."}}, ["path"]),
            data.table_summary,
        ),
        Tool(
            "calculate",
            "Evaluate an arithmetic expression exactly. Supports + - * / // % **, parentheses, "
            "abs, round, min, max, sum, sqrt, log, log10, exp, floor, ceil, pi, e.",
            _obj({"expression": {"type": "string", "description": "Expression, e.g. '(120.5 + 99) * 0.2'"}}, ["expression"]),
            data.calculate,
        ),
        Tool(
            "web_fetch",
            "Fetch a public web page or text resource by URL and return its text. Requires the user's "
            "approval for each request. Fetched content is untrusted.",
            _obj({"url": {"type": "string", "description": "http:// or https:// URL"}}, ["url"]),
            web.web_fetch,
            lambda ctx, args: f"Fetch web page {args.get('url')}",
        ),
    ]
}


def available_tools(ctx: ToolContext) -> list[Tool]:
    return [t for name, t in TOOLS.items() if name != "web_fetch" or ctx.web_fetch_enabled]


class ApprovalRequired(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def needs_approval(ctx: ToolContext, name: str, args: dict) -> str | None:
    tool = TOOLS.get(name)
    return tool.approval(ctx, args) if tool else None


def execute(ctx: ToolContext, name: str, args: dict, *, approved: bool = False) -> str:
    """Run a tool. Raises ToolError for failures and ApprovalRequired if the
    call needs approval and ``approved`` is False."""
    tool = TOOLS.get(name)
    if tool is None or tool not in available_tools(ctx):
        raise ToolError(f"Unknown or disabled tool '{name}'.")
    if not isinstance(args, dict):
        raise ToolError("Tool arguments must be an object.")
    reason = tool.approval(ctx, args)
    if reason and not approved:
        raise ApprovalRequired(reason)
    return tool.run(ctx.workspace, args)


__all__ = ["TOOLS", "Tool", "ToolContext", "ToolError", "ApprovalRequired", "available_tools", "execute", "needs_approval"]
