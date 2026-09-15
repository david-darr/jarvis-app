"""Read-only structural extraction from Office documents, for the chat file
preview panel (David's ask 2026-09-15).

Everything happens locally. No document is uploaded to Office Online, Google,
or any other converter — the whole point of previewing a generated file in
the app is that it never leaves the machine, and a "preview" that posts the
user's spreadsheet to a third party would quietly undo that.

Pure-Python readers only (openpyxl / python-docx / python-pptx). LibreOffice
is deliberately not a dependency: `soffice` is not on PATH here, and bundling
it into the installer would add hundreds of megabytes to a download whose
selling point is that it just works. The honest consequence is stated rather
than hidden — PPTX comes back as slide structure and text, NOT as rendered
images, so a slide's exact visual layout is not reproduced. The download
button next to the preview remains the way to see the real thing.

Nothing here executes anything
------------------------------
- Macros are never run. These libraries have no execution engine at all, and
  macro-enabled formats (.xlsm/.docm/.pptm) are not offered for preview in
  the first place (see core/chat_artifacts.py) rather than relying on that.
- Formulas are never evaluated. openpyxl is opened with data_only=True, which
  returns the value Excel last CACHED for a cell. A file written by a library
  rather than by Excel may have no cached value, in which case the cell reads
  blank — that is reported as blank, not guessed at.
- External workbook links are not followed (keep_links=False), and no reader
  here fetches a remote relationship, image, or stylesheet.

Bounded work
------------
OOXML files are zip archives, so an attacker-supplied one can be tiny on disk
and enormous in memory. _guard_archive() checks entry count, declared total
size, and compression ratio BEFORE any parser touches the file, and every
extractor caps how much it will walk. The declared sizes come from the
archive's own central directory and a hostile file can lie about them, so the
per-extractor caps below are the real ceiling, not the header check.
"""
import csv
import io
import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


class PreviewUnavailable(Exception):
    """Raised with a message meant to be shown to the user as-is."""


# Archive guards. Generous enough for a genuine deck or workbook, small
# enough that a zip bomb is refused instead of being expanded into memory.
MAX_ENTRIES = 3000
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200

# Per-format rendering caps. A preview is for orientation, not for reading a
# 100k-row export in a side panel.
MAX_SHEETS = 20
MAX_ROWS = 500
MAX_COLS = 50
MAX_DOC_BLOCKS = 2000
MAX_SLIDES = 200
MAX_SHAPES_PER_SLIDE = 60
MAX_CELL_CHARS = 2000
MAX_CSV_ROWS = 1000
MAX_CSV_BYTES = 8 * 1024 * 1024


def _guard_archive(path: Path) -> None:
    """Refuse an archive that would cost too much to expand, before handing
    it to a parser that would happily try."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                raise PreviewUnavailable("This file has too many internal parts to preview safely.")
            total = 0
            for info in infos:
                total += info.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise PreviewUnavailable("This file expands to too much data to preview safely.")
                if info.compress_size > 0 and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                    raise PreviewUnavailable("This file's compression looks unsafe to expand.")
    except zipfile.BadZipFile:
        raise PreviewUnavailable("This file isn't a readable Office document.")


def _clip(text: str) -> str:
    text = (text or "").replace("\x00", "")
    return text[:MAX_CELL_CHARS]


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return _clip(str(value))


def extract_xlsx(path: Path) -> dict:
    try:
        import openpyxl
    except ImportError:
        raise PreviewUnavailable("Spreadsheet previews need the openpyxl package.")
    _guard_archive(path)
    # read_only keeps memory bounded on a large sheet; data_only returns
    # cached values so no formula is ever evaluated; keep_links=False stops
    # references to other workbooks being resolved.
    try:
        book = openpyxl.load_workbook(path, data_only=True, read_only=True, keep_links=False)
    except Exception as e:
        logger.info("xlsx preview failed for %s: %s", path.name, e)
        raise PreviewUnavailable("This spreadsheet couldn't be read.")
    sheets = []
    # Captured before close(): a closed read-only workbook is not safe to
    # read attributes from afterwards.
    names = list(book.sheetnames)
    try:
        for name in names[:MAX_SHEETS]:
            ws = book[name]
            # ws.max_row/max_column are the sheet's own declared extent, which
            # is how the UI can say "showing the first 500 rows" honestly
            # rather than implying the file ends there.
            total_rows = ws.max_row or 0
            total_cols = ws.max_column or 0
            # Bound by the sheet's real width, not the cap: passing max_col
            # unconditionally pads every row out to 50 entries, so a
            # four-column sheet rendered as forty-six empty columns.
            col_limit = min(total_cols, MAX_COLS) if total_cols else MAX_COLS
            rows = [[_cell_text(v) for v in row]
                    for row in ws.iter_rows(max_row=MAX_ROWS, max_col=col_limit, values_only=True)]
            # Trailing all-empty rows are an artifact of the declared extent,
            # not content worth showing.
            while rows and not any(cell for cell in rows[-1]):
                rows.pop()
            sheets.append({
                "name": name,
                "rows": rows,
                "total_rows": total_rows or len(rows),
                "total_cols": total_cols or col_limit,
                "truncated_rows": bool(total_rows > len(rows)),
                "truncated_cols": bool(total_cols > MAX_COLS),
            })
    finally:
        book.close()
    return {"kind": "xlsx", "sheets": sheets, "truncated_sheets": len(names) > MAX_SHEETS}


def extract_docx(path: Path) -> dict:
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        raise PreviewUnavailable("Document previews need the python-docx package.")
    _guard_archive(path)
    try:
        document = docx.Document(str(path))
    except Exception as e:
        logger.info("docx preview failed for %s: %s", path.name, e)
        raise PreviewUnavailable("This document couldn't be read.")

    blocks, truncated = [], False
    # Walking body children rather than document.paragraphs keeps paragraphs
    # and tables in their real reading order; iterating the two collections
    # separately would move every table to the end.
    body = document.element.body
    for child in body.iterchildren():
        if len(blocks) >= MAX_DOC_BLOCKS:
            truncated = True
            break
        tag = child.tag.split("}")[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = _clip(paragraph.text).strip()
            if not text:
                continue
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            level = 0
            if style.startswith("Heading"):
                suffix = style.replace("Heading", "").strip()
                level = int(suffix) if suffix.isdigit() else 1
            blocks.append({
                "type": "heading" if level else ("list" if style.startswith("List") else "paragraph"),
                "level": min(level, 6),
                "text": text,
            })
        elif tag == "tbl":
            table = Table(child, document)
            rows = []
            for row in table.rows[:MAX_ROWS]:
                rows.append([_clip(cell.text).strip() for cell in row.cells[:MAX_COLS]])
            if rows:
                blocks.append({"type": "table", "rows": rows})
    return {"kind": "docx", "blocks": blocks, "truncated": truncated}


def extract_pptx(path: Path) -> dict:
    try:
        from pptx import Presentation
    except ImportError:
        raise PreviewUnavailable("Slide previews need the python-pptx package.")
    _guard_archive(path)
    try:
        deck = Presentation(str(path))
    except Exception as e:
        logger.info("pptx preview failed for %s: %s", path.name, e)
        raise PreviewUnavailable("This presentation couldn't be read.")

    slides = []
    for index, slide in enumerate(deck.slides, start=1):
        if index > MAX_SLIDES:
            break
        title, body, tables = "", [], []
        # The title placeholder is identified structurally rather than by
        # assuming the first shape is the title, which is often wrong.
        try:
            if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                title = _clip(slide.shapes.title.text).strip()
        except Exception:
            title = ""
        for shape in list(slide.shapes)[:MAX_SHAPES_PER_SLIDE]:
            try:
                if getattr(shape, "has_table", False) and shape.has_table:
                    # list() first: python-pptx's row and cell collections
                    # index by int only, and slicing them raises TypeError —
                    # which the guard below would swallow, dropping every
                    # table from the preview without a trace.
                    rows = [[_clip(cell.text).strip() for cell in list(row.cells)[:MAX_COLS]]
                            for row in list(shape.table.rows)[:MAX_ROWS]]
                    if rows:
                        tables.append(rows)
                    continue
                if not getattr(shape, "has_text_frame", False) or not shape.has_text_frame:
                    continue
                text = _clip(shape.text).strip()
                if not text or text == title:
                    continue
                for line in text.split("\n"):
                    line = line.strip()
                    if line:
                        body.append(line)
            except Exception:
                # One malformed shape must not lose the whole slide.
                continue
        notes = ""
        try:
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = _clip(slide.notes_slide.notes_text_frame.text).strip()
        except Exception:
            notes = ""
        slides.append({"index": index, "title": title, "body": body, "tables": tables, "notes": notes})
    return {
        "kind": "pptx",
        "slides": slides,
        "truncated": len(deck.slides._sldIdLst) > MAX_SLIDES if hasattr(deck.slides, "_sldIdLst") else False,
        # Surfaced in the UI. Text and structure are real; visual layout,
        # theming, and images are not reproduced without a renderer.
        "layout_fidelity": False,
    }


def extract_csv(path: Path) -> dict:
    """Parsed with the csv module rather than split on commas, so quoted
    fields containing commas or newlines survive intact — the whole reason
    this goes through the backend instead of being split in the browser."""
    size = path.stat().st_size
    if size > MAX_CSV_BYTES:
        raise PreviewUnavailable("This file is too large to preview as a table.")
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise PreviewUnavailable("This file's text encoding couldn't be read.")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # a single-column file sniffs as nothing; comma is the sane default
    rows, truncated = [], False
    for row in csv.reader(io.StringIO(text), dialect):
        if len(rows) >= MAX_CSV_ROWS:
            truncated = True
            break
        rows.append([_clip(cell) for cell in row[:MAX_COLS]])
    return {"kind": "csv", "rows": rows, "truncated": truncated}


_EXTRACTORS = {
    ".xlsx": extract_xlsx,
    ".docx": extract_docx,
    ".pptx": extract_pptx,
    ".csv": extract_csv,
}

# What core/chat_artifacts.py offers a structured preview for. Macro-enabled
# variants are deliberately absent: these readers cannot execute a macro, but
# not offering them at all is a clearer boundary than relying on that, and
# such a file still downloads normally.
PREVIEWABLE = frozenset(_EXTRACTORS)


def extract(path: Path, extension: str) -> dict:
    extractor = _EXTRACTORS.get(extension.lower())
    if extractor is None:
        raise PreviewUnavailable("This file type has no structured preview.")
    return extractor(Path(path))
