"""Workspace sandbox and file tools.

Every path supplied by the model is resolved with ``os.path.realpath`` (which
follows symlinks) and must stay inside the workspace root. Files are opened
with ``O_NOFOLLOW`` so a final path component cannot be swapped for a symlink
after validation.
"""

from __future__ import annotations

import os
from pathlib import Path


class ToolError(Exception):
    """An error reported back to the model (and shown to the user)."""


MAX_READ_CHARS = 100_000
MAX_WRITE_BYTES = 5_000_000
MAX_LIST_ENTRIES = 500
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(os.path.realpath(root))

    def resolve(self, rel: str | None) -> Path:
        rel = (rel or ".").strip() or "."
        if "\x00" in rel:
            raise ToolError("Invalid path.")
        if os.path.isabs(rel) or rel.startswith("~") or (len(rel) > 1 and rel[1] == ":"):
            raise ToolError(f"'{rel}' is an absolute path. Use a path relative to the workspace.")
        real = Path(os.path.realpath(self.root / rel))
        if real != self.root and self.root not in real.parents:
            raise ToolError(f"Access denied: '{rel}' is outside the workspace.")
        return real

    def display(self, path: Path) -> str:
        rel = path.relative_to(self.root).as_posix()
        return rel or "."

    def _open_read(self, path: Path) -> bytes:
        try:
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        except FileNotFoundError:
            raise ToolError(f"File not found: {self.display(path)}") from None
        except IsADirectoryError:
            raise ToolError(f"'{self.display(path)}' is a directory.") from None
        except OSError as e:
            raise ToolError(f"Cannot open '{self.display(path)}': {e.strerror}") from None
        with os.fdopen(fd, "rb") as f:
            return f.read(MAX_WRITE_BYTES + 1)

    def read_text(self, rel: str) -> str:
        path = self.resolve(rel)
        if path.is_dir():
            raise ToolError(f"'{rel}' is a directory. Use list_files instead.")
        data = self._open_read(path)
        if b"\x00" in data[:8192]:
            raise ToolError(f"'{rel}' appears to be a binary file and cannot be read as text.")
        return data.decode("utf-8", errors="replace")

    def exists(self, rel: str) -> bool:
        return self.resolve(rel).exists()

    def write_text(self, rel: str, content: str) -> int:
        path = self.resolve(rel)
        if path == self.root or path.is_dir():
            raise ToolError(f"'{rel}' is a directory.")
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError("Content is too large to write (limit 5 MB).")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-validate after creating directories (guards against a racing symlink).
        if self.resolve(rel) != path:
            raise ToolError("Access denied: path changed during write.")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return len(data)

    def delete(self, rel: str) -> None:
        path = self.resolve(rel)
        if path == self.root:
            raise ToolError("Cannot delete the workspace itself.")
        if path.is_dir():
            raise ToolError("Deleting directories is not supported.")
        if not path.exists():
            raise ToolError(f"File not found: {rel}")
        path.unlink()

    def list(self, rel: str = ".", recursive: bool = True) -> list[dict]:
        base = self.resolve(rel)
        if not base.is_dir():
            raise ToolError(f"'{rel}' is not a directory.")
        entries: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames.sort()
            for name in dirnames + sorted(filenames):
                full = Path(dirpath) / name
                if full.is_symlink():
                    try:
                        self.resolve(self.display(full))
                    except ToolError:
                        continue  # hide symlinks that point outside the workspace
                try:
                    is_dir = full.is_dir()
                    size = None if is_dir else full.stat().st_size
                except OSError:
                    continue  # dangling symlink, or removed while listing
                entries.append({"path": self.display(full), "type": "dir" if is_dir else "file", "size": size})
                if len(entries) >= MAX_LIST_ENTRIES:
                    return entries
            if not recursive:
                break
        return entries


# -- tool implementations ----------------------------------------------------


def list_files(ws: Workspace, args: dict) -> str:
    entries = ws.list(args.get("path") or ".", bool(args.get("recursive", True)))
    if not entries:
        return "(empty directory)"
    lines = [
        f"{e['path']}/" if e["type"] == "dir" else f"{e['path']}  ({e['size']} bytes)" for e in entries
    ]
    if len(entries) >= MAX_LIST_ENTRIES:
        lines.append(f"... listing truncated at {MAX_LIST_ENTRIES} entries")
    return "\n".join(lines)


def read_file(ws: Workspace, args: dict) -> str:
    path = _require(args, "path")
    text = ws.read_text(path)
    offset = max(int(args.get("offset") or 0), 0)
    chunk = text[offset : offset + MAX_READ_CHARS]
    if offset + MAX_READ_CHARS < len(text):
        chunk += (
            f"\n\n[Truncated: showing characters {offset}-{offset + len(chunk)} of {len(text)}. "
            f"Call read_file again with offset={offset + MAX_READ_CHARS} to continue.]"
        )
    return chunk


def write_file(ws: Workspace, args: dict) -> str:
    path = _require(args, "path")
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("'content' must be a string.")
    existed = ws.exists(path)
    size = ws.write_text(path, content)
    return f"{'Overwrote' if existed else 'Created'} {path} ({size} bytes)."


def delete_file(ws: Workspace, args: dict) -> str:
    path = _require(args, "path")
    ws.delete(path)
    return f"Deleted {path}."


def _require(args: dict, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"Missing required argument '{name}'.")
    return value
