# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Read-only text extraction from DOCX, XLSX and PDF files in the workspace.

Nothing in a document is ever executed or followed: macros (vbaProject.bin) are not read,
formulas are not calculated (XLSX gives the values Excel saved), external links and embedded
objects are ignored, and PDF scripts and actions are not run. Every limit below is checked
before or while reading, so an oversized or deliberately inflated file is refused, not loaded.

DOCX needs only the standard library. XLSX and PDF need the optional extra
``pip install 'nature-ai-worker[documents]'`` (openpyxl with defusedxml, and pypdf).
"""
from __future__ import annotations

import datetime
import io
import os
import re
import stat
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree

from .workspace import _BINARY, _NOFOLLOW, ToolError, Workspace

MAX_DOCUMENT_BYTES = 20_000_000       # the file itself (the upload limit)
MAX_ZIP_ENTRIES = 2_000               # DOCX/XLSX are ZIP archives
MAX_UNCOMPRESSED_BYTES = 100_000_000  # all their members together, as declared and as read
MAX_SHEETS = 50
MAX_ROWS_PER_SHEET = 10_000
MAX_CELLS = 200_000
MAX_PDF_PAGES = 300
MAX_EXTRACT_CHARS = 2_000_000         # extracted text; read_file then returns it in chunks

KINDS = {".docx": "DOCX", ".docm": "DOCX", ".xlsx": "XLSX", ".xlsm": "XLSX", ".pdf": "PDF"}
EXTRA = "pip install 'nature-ai-worker[documents]'"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def is_document(path: str) -> bool:
    return PurePosixPath(path.replace("\\", "/")).suffix.lower() in KINDS


def _read(ws: Workspace, rel: str) -> bytes:
    path = ws.resolve(rel)
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
    except FileNotFoundError:
        raise ToolError(f"File not found: {rel}") from None
    except OSError as e:
        raise ToolError(f"Cannot open '{rel}': {e.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ToolError(f"'{rel}' is not a regular file.")
        data = f.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ToolError(f"'{rel}' is too large to read (limit {MAX_DOCUMENT_BYTES // 1_000_000} MB).")
    return data


def _archive(data: bytes, rel: str) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ToolError(f"'{rel}' is not a valid Office file (damaged, or not DOCX/XLSX).") from None
    members = archive.infolist()
    if len(members) > MAX_ZIP_ENTRIES:
        raise ToolError(f"'{rel}' has too many parts to read safely ({len(members)}).")
    if sum(m.file_size for m in members) > MAX_UNCOMPRESSED_BYTES:
        raise ToolError(f"'{rel}' expands to more than {MAX_UNCOMPRESSED_BYTES // 1_000_000} MB; not read.")
    return archive


def _member(archive: zipfile.ZipFile, name: str, rel: str) -> bytes:
    try:
        with archive.open(name) as f:
            data = f.read(MAX_UNCOMPRESSED_BYTES + 1)  # the declared size may lie: bound what is read
    except KeyError:
        raise ToolError(f"'{rel}' has no {name}; it is not a Word document.") from None
    if len(data) > MAX_UNCOMPRESSED_BYTES:
        raise ToolError(f"'{rel}' expands to more than {MAX_UNCOMPRESSED_BYTES // 1_000_000} MB; not read.")
    return data


def _xml(data: bytes, rel: str) -> ElementTree.Element:
    # Office documents never need a DTD; refusing one rules out entity expansion and external entities.
    if re.search(rb"<!DOCTYPE|<!ENTITY", data[:4096] + data[-4096:], re.IGNORECASE) or b"<!ENTITY" in data:
        raise ToolError(f"'{rel}' contains a DTD, which Office documents do not use; not read.")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise ToolError(f"'{rel}' is damaged (invalid XML).") from None


def _paragraph(p: ElementTree.Element) -> str:
    parts = []
    for node in p.iter():
        if node.tag == _W + "t" and node.text:
            parts.append(node.text)
        elif node.tag == _W + "tab":
            parts.append("\t")
        elif node.tag in (_W + "br", _W + "cr"):
            parts.append("\n")
    text = "".join(parts)
    style = p.find(f"{_W}pPr/{_W}pStyle")
    level = re.fullmatch(r"Heading\s*(\d)", (style.get(_W + "val") or "") if style is not None else "")
    return f"{'#' * int(level.group(1))} {text}" if level and text else text


def _docx(data: bytes, rel: str) -> tuple[str, str]:
    root = _xml(_member(_archive(data, rel), "word/document.xml", rel), rel)
    body = root.find(_W + "body")
    lines, tables, paragraphs = [], 0, 0
    for block in (list(body) if body is not None else []):
        if block.tag == _W + "p":
            paragraphs += 1
            lines.append(_paragraph(block))
        elif block.tag == _W + "tbl":
            tables += 1
            lines.append("")
            for row in block.iter(_W + "tr"):
                cells = [" ".join(_paragraph(p) for p in tc.iter(_W + "p")).strip() for tc in row.iter(_W + "tc")]
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
    return "\n".join(lines), f"{paragraphs} paragraphs, {tables} tables"


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return str(value)


def _workbook(data: bytes, rel: str):
    _archive(data, rel)  # the same archive limits before openpyxl reads it
    try:
        import defusedxml  # noqa: F401  openpyxl parses with it when installed
        import openpyxl
    except ImportError:
        raise ToolError(f"Reading Excel files needs the optional document support: {EXTRA}") from None
    try:
        return openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
    except Exception as e:  # noqa: BLE001 - any parser failure is a damaged or unsupported file
        raise ToolError(f"'{rel}' could not be read as an Excel workbook ({type(e).__name__}).") from None


def sheet_rows(ws: Workspace, rel: str, sheet: str | None = None) -> list[list[str]]:
    """The cell values of one worksheet (the first, unless named), within the row and cell limits."""
    book = _workbook(_read(ws, rel), rel)
    try:
        names = book.sheetnames
        if sheet is not None and sheet not in names:
            raise ToolError(f"'{rel}' has no sheet '{sheet}'. Sheets: {', '.join(names)}")
        rows = []
        for row in book[sheet or names[0]].iter_rows(values_only=True):
            if len(rows) >= MAX_ROWS_PER_SHEET:
                break
            rows.append([_cell(v) for v in row])
        return rows
    finally:
        book.close()


def _xlsx(data: bytes, rel: str) -> tuple[str, str]:
    book = _workbook(data, rel)
    lines, cells, notes = [], 0, []
    try:
        names = book.sheetnames
        if len(names) > MAX_SHEETS:
            notes.append(f"only the first {MAX_SHEETS} of {len(names)} sheets")
        for name in names[:MAX_SHEETS]:
            lines.append(f"## Sheet: {name}")
            count = 0
            for row in book[name].iter_rows(values_only=True):
                if count >= MAX_ROWS_PER_SHEET or cells >= MAX_CELLS:
                    notes.append(f"sheet '{name}' cut at {count} rows")
                    break
                values = [_cell(v) for v in row]
                while values and values[-1] == "":
                    values.pop()
                cells += len(values)
                count += 1
                lines.append("\t".join(values))
            lines.append("")
    finally:
        book.close()
    return "\n".join(lines), f"{min(len(names), MAX_SHEETS)} sheets, tab-separated; formulas as saved values" + (
        f"; {', '.join(notes)}" if notes else "")


def _pdf(data: bytes, rel: str) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise ToolError(f"Reading PDF files needs the optional document support: {EXTRA}") from None
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ToolError(f"'{rel}' is password-protected; it cannot be read.")
        total = len(reader.pages)
        pages = []
        for i in range(min(total, MAX_PDF_PAGES)):
            pages.append(f"## Page {i + 1}\n{(reader.pages[i].extract_text() or '').strip()}")
    except ToolError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as e:
        raise ToolError(f"'{rel}' could not be read as a PDF ({type(e).__name__}).") from None
    text = "\n\n".join(pages)
    note = f"{total} pages" + (f"; only the first {MAX_PDF_PAGES}" if total > MAX_PDF_PAGES else "")
    if not text.replace("## Page", "").strip(" \n0123456789"):
        note += "; no text layer (a scanned document needs OCR, which is not supported)"
    return text, note


def extract(ws: Workspace, rel: str) -> tuple[str, str]:
    """(extracted text, a one-line header for the model)."""
    kind = KINDS[PurePosixPath(rel.replace("\\", "/")).suffix.lower()]
    data = _read(ws, rel)
    text, facts = {"DOCX": _docx, "XLSX": _xlsx, "PDF": _pdf}[kind](data, rel)
    if len(text) > MAX_EXTRACT_CHARS:
        text, facts = text[:MAX_EXTRACT_CHARS], facts + f"; text cut at {MAX_EXTRACT_CHARS:,} characters"
    header = (f"[Text extracted from {kind} file '{rel}' ({facts}). Read-only: formatting, images, macros "
              f"and embedded objects are not included.]\n")
    return text, header
