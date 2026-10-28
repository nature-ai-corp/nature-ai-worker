# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Document input: Windows CSV encodings, and read-only DOCX/XLSX/PDF extraction within limits.
Nothing in a document is executed: no macro, formula, external link or script."""
import builtins
import io
import os
import zipfile

import pytest

from nature_worker.tools import ToolContext, ToolError, execute
from nature_worker.tools import documents
from nature_worker.tools.workspace import Workspace

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def run(ws_dir, tool, **args):
    return execute(ToolContext(Workspace(ws_dir)), tool, args)


# -- encodings -------------------------------------------------------------------------

SALES = "region,owner,amount\nNorth,Müller,120\nSouth,José,80\nNorth,Zoë,50\n"


@pytest.mark.parametrize("encoding, bom, label", [
    ("cp1252", b"", "Windows-1252"),       # Excel "CSV" on Windows
    ("utf-16-le", b"\xff\xfe", "UTF-16"),   # Excel "Unicode text"
    ("utf-8", b"\xef\xbb\xbf", None),       # Excel "CSV UTF-8": BOM removed, no note
    ("utf-8", b"", None),
])
def test_windows_csv_encodings_are_read_correctly(tmp_path, encoding, bom, label):
    (tmp_path / "sales.csv").write_bytes(bom + SALES.encode(encoding))
    text = run(tmp_path, "read_file", path="sales.csv")
    assert "Müller" in text and "José" in text and "Zoë" in text and "﻿" not in text
    assert (f"[Decoded as {label}" in text) is (label is not None)
    summary = run(tmp_path, "table_summary", path="sales.csv")
    assert "Rows: 3" in summary and "sum=250" in summary and "region" in summary.split("\n")[1]


@pytest.mark.parametrize("data, message", [
    (b"name\nM\x81ller\n", "unsupported text encoding"),            # 0x81 is undefined in Windows-1252
    (b"\xef\xbb\xbfname\nM\xfcller\n", r"UTF-8 byte-order mark but is not valid UTF-8 \(at byte 9\)"),  # a UTF-8 BOM, then Latin-1 bytes
    (b"\xff\xfe" + "name\n".encode("utf-16-le") + b"\x00\xd8", "not valid UTF-16"),  # lone surrogate
])
def test_undecodable_text_is_reported_never_silently_replaced(tmp_path, data, message):
    (tmp_path / "bad.csv").write_bytes(data)
    for tool, args in (("read_file", {}), ("table_summary", {})):
        with pytest.raises(ToolError, match=message) as failure:
            run(tmp_path, tool, path="bad.csv", **args)
        assert "\ufffd" not in str(failure.value)
    assert "Save it again as UTF-8" in str(failure.value)
    assert run(tmp_path, "search_text", query="name") == "No matches found."  # skipped, not garbled


def test_cyrillic_windows_1251_is_not_detectable_and_is_labelled(tmp_path):
    """Known limit: single-byte Windows code pages other than 1252 decode as 1252 (every byte is
    defined). The model is told the encoding, so wrong letters are not presented as UTF-8."""
    (tmp_path / "ru.csv").write_bytes("город\n".encode("cp1251"))
    assert "[Decoded as Windows-1252" in run(tmp_path, "read_file", path="ru.csv")


def test_a_real_binary_file_is_still_refused(tmp_path):
    (tmp_path / "image.bin").write_bytes(b"PNG\x00\x01\x02" * 100)
    with pytest.raises(ToolError, match="binary"):
        run(tmp_path, "read_file", path="image.bin")


# -- DOCX --------------------------------------------------------------------------------

def docx(path, body_xml: str, extra: dict | None = None, doctype: str = ""):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<?xml version="1.0"?>{doctype}<w:document xmlns:w="{W}"><w:body>'
                                        f"{body_xml}</w:body></w:document>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)


def para(text, style=None):
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


TABLE = ("<w:tbl><w:tr><w:tc>" + para("Region") + "</w:tc><w:tc>" + para("Amount") + "</w:tc></w:tr>"
         "<w:tr><w:tc>" + para("North") + "</w:tc><w:tc>" + para("170") + "</w:tc></w:tr></w:tbl>")


def test_docx_text_headings_and_tables(tmp_path):
    docx(tmp_path / "brief.docx", para("Q3 brief", "Heading1") + para("Focus on the South.") + TABLE)
    text = run(tmp_path, "read_file", path="brief.docx")
    assert "[Text extracted from DOCX file 'brief.docx' (2 paragraphs, 1 tables)" in text
    assert "# Q3 brief" in text and "Focus on the South." in text and "| North | 170 |" in text


def test_macros_in_a_macro_enabled_document_are_ignored(tmp_path):
    docx(tmp_path / "macro.docm", para("Visible text"), extra={"word/vbaProject.bin": b"\x00Attribute VB_Name"})
    text = run(tmp_path, "read_file", path="macro.docm")
    assert "Visible text" in text and "VB_Name" not in text and "macros" in text


def test_a_docx_with_a_dtd_is_refused(tmp_path):
    bomb = '<!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
    docx(tmp_path / "bomb.docx", para("&b;"), doctype=bomb)
    with pytest.raises(ToolError, match="DTD"):
        run(tmp_path, "read_file", path="bomb.docx")


def test_inflated_and_overfull_archives_are_refused_before_reading(tmp_path, monkeypatch):
    path = tmp_path / "big.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>' + " " * 3_000_000 + "</w:body></w:document>")
    monkeypatch.setattr(documents, "MAX_UNCOMPRESSED_BYTES", 1_000_000)
    with pytest.raises(ToolError, match="expands to more than"):
        run(tmp_path, "read_file", path="big.docx")
    monkeypatch.setattr(documents, "MAX_ZIP_ENTRIES", 3)
    docx(tmp_path / "many.docx", para("x"), extra={f"part{i}.xml": "<a/>" for i in range(5)})
    with pytest.raises(ToolError, match="too many parts"):
        run(tmp_path, "read_file", path="many.docx")


def test_oversized_and_damaged_documents_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_DOCUMENT_BYTES", 1000)
    (tmp_path / "huge.pdf").write_bytes(b"%PDF-1.4\n" + b"0" * 2000)
    with pytest.raises(ToolError, match="too large"):
        run(tmp_path, "read_file", path="huge.pdf")
    (tmp_path / "fake.docx").write_text("not a zip")
    with pytest.raises(ToolError, match="not a valid Office file"):
        run(tmp_path, "read_file", path="fake.docx")


def test_a_document_link_out_of_the_workspace_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    docx(outside / "secret.docx", para("secret"))
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        os.symlink(outside / "secret.docx", ws / "link.docx")
    except OSError:
        pytest.skip("symbolic links not permitted")
    with pytest.raises(ToolError, match="outside the workspace"):
        run(ws, "read_file", path="link.docx")


def test_long_extracted_text_is_bounded_and_chunked(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_EXTRACT_CHARS", 500)
    docx(tmp_path / "long.docx", "".join(para(f"line {i:04d}") for i in range(200)))
    text = run(tmp_path, "read_file", path="long.docx")
    assert "text cut at 500 characters" in text and "line 0199" not in text


# -- XLSX --------------------------------------------------------------------------------

openpyxl = pytest.importorskip("openpyxl")


def xlsx(path):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Sales"
    for row in (["region", "amount"], ["North", 120], ["South", 80], ["North", 50]):
        sheet.append(row)
    sheet["D1"] = "=SUM(B2:B4)"                    # a formula without a saved value: never calculated
    sheet["E1"] = '=HYPERLINK("http://evil.invalid","x")'
    notes = book.create_sheet("Notes")
    notes.append(["topic", "region"])
    notes.append(["Q3 focus", "South"])
    book.save(path)


def test_xlsx_sheets_as_text_and_tables_without_evaluating_formulas(tmp_path):
    xlsx(tmp_path / "sales.xlsx")
    text = run(tmp_path, "read_file", path="sales.xlsx")
    assert "## Sheet: Sales" in text and "North\t120" in text and "## Sheet: Notes" in text
    assert "formulas as saved values" in text
    assert "250" not in text and "evil.invalid" not in text      # neither calculated nor followed
    summary = run(tmp_path, "table_summary", path="sales.xlsx")
    assert "Rows: 3" in summary and "sum=250" in summary
    assert "Q3 focus" in run(tmp_path, "table_summary", path="sales.xlsx", sheet="Notes")
    with pytest.raises(ToolError, match="no sheet 'Missing'"):
        run(tmp_path, "table_summary", path="sales.xlsx", sheet="Missing")


def test_xlsx_rows_and_cells_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_ROWS_PER_SHEET", 2)
    xlsx(tmp_path / "sales.xlsx")
    text = run(tmp_path, "read_file", path="sales.xlsx")
    assert "sheet 'Sales' cut at 2 rows" in text and "South\t80" not in text


def test_without_the_optional_extra_the_user_is_told_what_to_install(tmp_path, monkeypatch):
    xlsx(tmp_path / "sales.xlsx")
    real = builtins.__import__

    def missing(name, *a, **kw):
        if name in ("openpyxl", "pypdf", "defusedxml"):
            raise ImportError(name)
        return real(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ToolError, match=r"nature-ai-worker\[documents\]"):
        run(tmp_path, "read_file", path="sales.xlsx")


# -- PDF ---------------------------------------------------------------------------------

pypdf = pytest.importorskip("pypdf")


def pdf(text_lines: list[str]) -> bytes:
    """A minimal one-page PDF with a text layer."""
    stream = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({t}) Tj 0 -16 Td" for t in text_lines) + " ET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               "/Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def test_pdf_text_by_page(tmp_path):
    (tmp_path / "terms.pdf").write_bytes(pdf(["Payment within 30 days", "Penalty 2 percent"]))
    text = run(tmp_path, "read_file", path="terms.pdf")
    assert "(1 pages)" in text and "## Page 1" in text and "Payment within 30 days" in text


def test_encrypted_and_damaged_pdfs_are_refused(tmp_path):
    writer = pypdf.PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    (tmp_path / "locked.pdf").write_bytes(buf.getvalue())
    with pytest.raises(ToolError, match="password-protected"):
        run(tmp_path, "read_file", path="locked.pdf")
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4\ngarbage")
    with pytest.raises(ToolError, match="could not be read as a PDF"):
        run(tmp_path, "read_file", path="broken.pdf")


def test_pdf_pages_are_bounded_and_scans_are_explained(tmp_path, monkeypatch):
    writer = pypdf.PdfWriter()
    for _ in range(3):
        writer.add_blank_page(100, 100)
    buf = io.BytesIO()
    writer.write(buf)
    (tmp_path / "scan.pdf").write_bytes(buf.getvalue())
    monkeypatch.setattr(documents, "MAX_PDF_PAGES", 2)
    text = run(tmp_path, "read_file", path="scan.pdf")
    assert "3 pages; only the first 2" in text and "no text layer" in text and "## Page 3" not in text
