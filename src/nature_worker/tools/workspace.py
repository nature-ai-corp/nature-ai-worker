# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Workspace sandbox and file tools.

Every path supplied by the model is resolved with ``os.path.realpath`` (which
follows symlinks) and must stay inside the workspace root. Files are opened
with ``O_NOFOLLOW`` so a final path component cannot be swapped for a symlink
after validation.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path


class ToolError(Exception):
    """An error reported back to the model (and shown to the user)."""


MAX_READ_CHARS = 100_000
MAX_WRITE_BYTES = 5_000_000
MAX_LIST_ENTRIES = 500
MAX_APPEND_BYTES = 1_000_000   # one append_file call; the file as a whole stays within MAX_WRITE_BYTES
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)


@dataclass(frozen=True)
class Fingerprint:
    """Identity and exact content of a file the Worker wrote: the same path is only the same
    file if device, inode and SHA-256 all match (a replaced, re-linked or edited file does not)."""
    dev: int
    ino: int
    size: int
    sha256: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Fingerprint | None":
        try:
            return cls(int(d["dev"]), int(d["ino"]), int(d["size"]), str(d["sha256"]))
        except (KeyError, TypeError, ValueError):
            return None


class FileChanged(ToolError):
    """The file is no longer exactly the one the Worker wrote."""


def _fingerprint(fd: int) -> Fingerprint:
    st = os.fstat(fd)
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1 << 20):
        digest.update(chunk)
    return Fingerprint(st.st_dev, st.st_ino, st.st_size, digest.hexdigest())


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


# Encodings tried in order. Decoding is always strict: a byte sequence that is not valid in the
# detected encoding is reported, never silently replaced.
_BOMS = ((b"\xef\xbb\xbf", "utf-8", "UTF-8 (with BOM)"), (b"\xff\xfe", "utf-16-le", "UTF-16"),
         (b"\xfe\xff", "utf-16-be", "UTF-16"))


def decode_text(data: bytes, rel: str = "file") -> tuple[str, str]:
    """(text, encoding name). UTF-8 or UTF-16 with a byte-order mark, else UTF-8, else
    Windows-1252 (what Excel writes for "CSV" on Windows). Anything else is refused."""
    for bom, codec, name in _BOMS:
        if data.startswith(bom):
            try:
                return data[len(bom):].decode(codec), name
            except UnicodeDecodeError as e:
                plain = name.replace(" (with BOM)", "")
                raise ToolError(f"'{rel}' starts with a {plain} byte-order mark but is not valid {plain} "
                                f"(at byte {e.start + len(bom)}). Save it again as UTF-8.") from None
    if b"\x00" in data[:8192]:
        raise ToolError(f"'{rel}' appears to be a binary file (or UTF-16/32 without a byte-order mark) "
                        "and cannot be read as text.")
    try:
        return data.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "Windows-1252"
    except UnicodeDecodeError as e:
        raise ToolError(f"'{rel}' is in an unsupported text encoding: it is neither UTF-8, UTF-16 nor "
                        f"Windows-1252 (byte 0x{data[e.start]:02X} at position {e.start}). Save it again "
                        "as UTF-8, for example with Excel's \"CSV UTF-8\" format.") from None


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(os.path.realpath(root))

    def resolve(self, rel: str | None) -> Path:
        rel = (rel or ".").strip() or "."
        if "\x00" in rel:
            raise ToolError("Invalid path.")
        if os.path.isabs(rel) or rel.startswith(("/", "\\", "~")) or (len(rel) > 1 and rel[1] == ":"):
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
        return self.read_text_info(rel)[0]

    def read_text_info(self, rel: str) -> tuple[str, str]:
        """The text of a file and the encoding it was decoded with: UTF-8 (with or without BOM),
        UTF-16 with BOM (Excel "Unicode text"), else Windows-1252 (Excel's CSV on Windows)."""
        path = self.resolve(rel)
        if path.is_dir():
            raise ToolError(f"'{rel}' is a directory. Use list_files instead.")
        return decode_text(self._open_read(path), rel)

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

    def fingerprint(self, rel: str) -> Fingerprint | None:
        """The fingerprint of a regular file (never through a final symlink), else None."""
        path = self.resolve(rel)
        try:
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
        except OSError:
            return None
        try:
            return _fingerprint(fd) if stat.S_ISREG(os.fstat(fd).st_mode) else None
        finally:
            os.close(fd)

    def write_checked(self, rel: str, content: str, *, mode: str,
                      expect: Fingerprint | None = None) -> tuple[str, Fingerprint]:
        """Write and return (canonical path, new fingerprint). ``mode``:

        - ``create``: only if nothing exists there (O_EXCL), so a file that appeared since the
          check is never overwritten;
        - ``replace`` / ``append``: an existing regular file, opened without following a final
          symlink; with ``expect``, only if it is still exactly that file (checked on the open
          descriptor, so it cannot be swapped between the check and the write).
        """
        path = self.resolve(rel)
        if path == self.root or path.is_dir():
            raise ToolError(f"'{rel}' is a directory.")
        data = content.encode("utf-8")
        limit = MAX_APPEND_BYTES if mode == "append" else MAX_WRITE_BYTES
        if len(data) > limit:
            raise ToolError(f"Content is too large for one call (limit {limit // 1_000_000} MB).")
        if mode == "create":
            path.parent.mkdir(parents=True, exist_ok=True)
            if self.resolve(rel) != path:
                raise ToolError("Access denied: path changed during write.")
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o644)
            except FileExistsError:
                raise FileChanged(f"'{rel}' was created by someone else just now; it was not changed.") from None
        else:
            try:
                fd = os.open(path, os.O_RDWR | _NOFOLLOW | _BINARY)
            except FileNotFoundError:
                raise FileChanged(f"'{rel}' no longer exists; it was not changed.") from None
            except OSError as e:
                raise ToolError(f"Cannot open '{self.display(path)}': {e.strerror}") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ToolError(f"'{rel}' is not a regular file.")
            if expect is not None and _fingerprint(fd) != expect:
                raise FileChanged(f"'{rel}' changed after the Worker last wrote it.")
            if mode == "append":
                if os.fstat(fd).st_size + len(data) > MAX_WRITE_BYTES:
                    raise ToolError("The file would exceed the 5 MB limit; it was not changed.")
                os.lseek(fd, 0, os.SEEK_END)
            else:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
            _write_all(fd, data)
            os.fsync(fd)
            return self.display(path), _fingerprint(fd)
        finally:
            os.close(fd)

    def replace_checked(self, rel: str, old: str, new: str, *,
                        expect: Fingerprint | None = None) -> tuple[str, Fingerprint, int]:
        """Replace the one exact occurrence of ``old`` with ``new`` and return (canonical path,
        new fingerprint, line number of the change). The text is decoded and encoded again in the
        file's own encoding (byte-order mark included); nothing else in the file changes.

        The new content goes to a temporary file in the same folder, which then replaces the
        original in one rename, so a reader sees the old file or the new one, never a mix. The
        original is read through one descriptor (never through a final symlink); with ``expect``
        it must still be exactly that file, and in every case it must be unchanged since it was
        read, checked again just before the rename.
        """
        if not old:
            raise ToolError("'old_text' must not be empty.")
        path = self.resolve(rel)
        if path == self.root or path.is_dir():
            raise ToolError(f"'{rel}' is a directory.")
        try:
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
        except FileNotFoundError:
            raise ToolError(f"File not found: {rel}") from None
        except OSError as e:
            raise ToolError(f"Cannot open '{self.display(path)}': {e.strerror}") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ToolError(f"'{rel}' is not a regular file.")
            if st.st_nlink > 1:
                # Replacing one name of a hard-linked file would leave the other names unchanged.
                raise ToolError(f"'{rel}' has other hard links; it was not changed.")
            before = _fingerprint(fd)
            if expect is not None and before != expect:
                raise FileChanged(f"'{rel}' changed after the Worker last wrote it.")
            os.lseek(fd, 0, os.SEEK_SET)
            data = b""
            while chunk := os.read(fd, 1 << 20):
                data += chunk
                if len(data) > MAX_WRITE_BYTES:
                    raise ToolError("The file is too large to edit (limit 5 MB).")
        finally:
            os.close(fd)
        if hashlib.sha256(data).hexdigest() != before.sha256:
            raise FileChanged(f"'{rel}' changed while it was being read; it was not changed.")

        text, encoding = decode_text(data, rel)
        count = text.count(old)
        if count != 1:
            hint = ""
            if count == 0 and "\r\n" in text and "\n" in old and "\r\n" not in old:
                hint = " The file uses Windows line endings (\\r\\n)."
            raise ToolError(
                f"'old_text' was found {count} times in '{rel}'; it must match exactly once. Nothing was changed."
                + (" Use search_text or read_file to copy the exact text, including spaces and line breaks."
                   if count == 0 else " Include more of the surrounding text so it matches only one place.")
                + hint)
        at = text.index(old)
        result = text[:at] + new + text[at + len(old):]
        codec, bom = {"UTF-8": ("utf-8", b""), "UTF-8 (with BOM)": ("utf-8", b"\xef\xbb\xbf"),
                      "Windows-1252": ("cp1252", b"")}.get(encoding, (None, b""))
        if codec is None:   # UTF-16: the byte-order mark tells which byte order
            codec = "utf-16-le" if data.startswith(b"\xff\xfe") else "utf-16-be"
            bom = data[:2]
        try:
            out = bom + result.encode(codec)
        except UnicodeEncodeError as e:
            raise ToolError(f"'new_text' contains {e.object[e.start:e.end]!r}, which cannot be saved in this "
                            f"file's encoding ({encoding}). Nothing was changed.") from None
        if len(out) > MAX_WRITE_BYTES:
            raise ToolError("The file would exceed the 5 MB limit; it was not changed.")

        tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
        try:
            tfd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, stat.S_IMODE(st.st_mode))
        except OSError as e:
            raise ToolError(f"Cannot edit '{rel}': {e.strerror}. Nothing was changed.") from None
        try:
            try:
                _write_all(tfd, out)
                os.fsync(tfd)
                after = _fingerprint(tfd)
            finally:
                os.close(tfd)
            # Same folder (nothing re-linked on the way) and still the file that was read.
            if self.resolve(rel) != path or self.fingerprint(rel) != before:
                raise FileChanged(f"'{rel}' changed while it was being edited; it was not changed.")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return self.display(path), after, text.count("\n", 0, at) + 1

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
            escaping = set()
            for name in dirnames + sorted(filenames):
                full = Path(dirpath) / name
                try:
                    self.resolve(self.display(full))
                except ToolError:
                    # Hide links that point outside the workspace - symbolic links and,
                    # on Windows, NTFS junctions (which os.walk enters before Python 3.12) -
                    # and never descend into them.
                    escaping.add(name)
                    continue
                try:
                    is_dir = full.is_dir()
                    size = None if is_dir else full.stat().st_size
                except OSError:
                    continue  # dangling symlink, or removed while listing
                entries.append({"path": self.display(full), "type": "dir" if is_dir else "file", "size": size})
                if len(entries) >= MAX_LIST_ENTRIES:
                    return entries
            dirnames[:] = [d for d in dirnames if d not in escaping]
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


MIN_READ_CHARS = 2_000


def read_file(ws: Workspace, args: dict, budget: int | None = None) -> str:
    """One chunk of a file: MAX_READ_CHARS, or less when the model's context window is small, or
    ``limit`` characters when the model asks for a smaller part."""
    size = max(MIN_READ_CHARS, min(MAX_READ_CHARS, budget)) if budget else MAX_READ_CHARS
    path = _require(args, "path")
    from .documents import extract, is_document  # optional formats, read-only
    note = ""
    if is_document(path):
        text, note = extract(ws, path)
    else:
        text, encoding = ws.read_text_info(path)
        if encoding not in ("UTF-8", "UTF-8 (with BOM)"):
            note = f"[Decoded as {encoding}; the file is not UTF-8.]\n"
    offset = max(int(args.get("offset") or 0), 0)
    if args.get("limit"):
        size = min(size, max(int(args["limit"]), 1))   # one bounded part, never more than a chunk
    body = text[offset : offset + size]
    chunk = (note if offset == 0 else "") + body
    if offset + size < len(text):
        chunk += (
            f"\n\n[Truncated: showing characters {offset}-{offset + len(body)} of {len(text)}. "
            f"Call read_file again with offset={offset + size} to continue, or use search_text to find "
            "the part you need.]"
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
