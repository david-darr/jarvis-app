"""Read-only structural extraction from Office documents, for the chat file
preview panel (David's ask 2026-09-15).

Everything happens locally. No document is uploaded to Office Online, Google,
or any other converter — the whole point of previewing a generated file in
the app is that it never leaves the machine, and a "preview" that posts the
user's spreadsheet to a third party would quietly undo that.

Pure-Python readers only (openpyxl / python-docx / python-pptx). LibreOffice
is deliberately not a dependency: `soffice` is not on PATH here, and bundling
it into the installer would add hundreds of megabytes to a download whose
selling point is that it just works. PPTX supplies bounded geometry, text,
and raster pictures for an approximate layout, not an exact Office render.
The download button remains the way to see the exact slides.

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
import base64
import io
import logging
import math
import zipfile
from itertools import islice
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
MAX_IMAGE_BYTES = 1536 * 1024
MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000


class _ImageBudget:
    """Count encoded data URI bytes, so JSON's image payload stays bounded."""
    def __init__(self, enabled=True):
        self.used = 0
        self.enabled = enabled

    def read(self, blob):
        # Disabled for text-only callers (artifact comments), which would
        # otherwise decode and re-encode every picture just to read text.
        if not self.enabled or self.used >= MAX_TOTAL_IMAGE_BYTES:
            return None
        if blob.startswith(b"\x89PNG\r\n\x1a\n"):
            expected = "PNG"
        elif blob.startswith(b"\xff\xd8\xff"):
            expected = "JPEG"
        elif blob[:6] in (b"GIF87a", b"GIF89a"):
            expected = "GIF"
        elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            expected = "WEBP"
        else:
            return None
        try:
            from PIL import Image
            with Image.open(io.BytesIO(blob)) as picture:
                if picture.format != expected or picture.width * picture.height > MAX_IMAGE_PIXELS:
                    return None
                picture.seek(0)  # animated images are a static first-frame preview
                picture = picture.convert("RGBA")
                opaque = picture.getchannel("A").getextrema()[0] == 255
                # A photo saved as PNG easily passes the per-image cap at 1600 px,
                # so after the native format, fall back to JPEG when there is no
                # transparency, then smaller sizes.
                native = "JPEG" if expected == "JPEG" else "PNG"
                fallback = "JPEG" if opaque else native
                uri = None
                for edge, fmt in ((1600, native), (1600, fallback), (1200, fallback), (800, fallback)):
                    frame = picture.copy()
                    frame.thumbnail((edge, edge))
                    output = io.BytesIO()
                    (frame.convert("RGB") if fmt == "JPEG" else frame).save(output, format=fmt, **({"quality": 85} if fmt == "JPEG" else {}))
                    uri = "data:image/" + fmt.lower() + ";base64," + base64.b64encode(output.getvalue()).decode("ascii")
                    if len(uri) <= MAX_IMAGE_BYTES:
                        break
            size = len(uri)
            if size > MAX_IMAGE_BYTES or self.used + size > MAX_TOTAL_IMAGE_BYTES:
                return None
            self.used += size
            return uri
        except Exception:
            return None


def _rgb(color):
    """Only literal RGB; unresolved theme/indexed colors remain unknown."""
    try:
        from pptx.enum.dml import MSO_COLOR_TYPE
        return "#" + str(color.rgb).lower() if color.type == MSO_COLOR_TYPE.RGB else None
    except Exception:
        return None


def _solid_fill(fill):
    try:
        from pptx.enum.dml import MSO_FILL_TYPE
        return _rgb(fill.fore_color) if fill.type == MSO_FILL_TYPE.SOLID else None
    except Exception:
        return None


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


def _table_text(rows):
    # Do not join a potentially large grid just to clip its text excerpt.
    parts, remaining = [], MAX_CELL_CHARS
    for row_index, row in enumerate(rows):
        for col_index, cell in enumerate(row):
            value = ("\t" if col_index else "\n" if row_index else "") + cell
            parts.append(value[:remaining])
            remaining -= min(len(value), remaining)
            if not remaining:
                return "".join(parts)
    return "".join(parts)


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


def _doc_list(paragraph, document):
    from docx.oxml.ns import qn
    props = paragraph._p.pPr
    numbering = props.numPr if props is not None else None
    style = paragraph.style
    for _ in range(12):
        if numbering is not None or style is None:
            break
        props = style.element.pPr
        numbering = props.numPr if props is not None else None
        style = style.base_style
    name = paragraph.style.name if paragraph.style is not None else ""
    if numbering is None:
        if name.startswith(("List Bullet", "List Number")):
            return ("number" if "Number" in name else "bullet"), 0
        return None, 0
    level = int(numbering.ilvl.val) if numbering.ilvl is not None else 0
    kind = "bullet"
    try:
        num_id = numbering.numId.val
        if num_id == 0:
            return None, 0
        root = document.part.numbering_part.element
        num = next(n for n in root if n.tag == qn("w:num") and n.get(qn("w:numId")) == str(num_id))
        abstract_id = num.find(qn("w:abstractNumId")).get(qn("w:val"))
        abstract = next(n for n in root if n.tag == qn("w:abstractNum") and n.get(qn("w:abstractNumId")) == abstract_id)
        lvl = next(n for n in abstract if n.tag == qn("w:lvl") and n.get(qn("w:ilvl")) == str(level))
        fmt = lvl.find(qn("w:numFmt")).get(qn("w:val"))
        kind = "bullet" if fmt == "bullet" else "number"
    except Exception:
        if "Number" in name:
            kind = "number"
    return kind, min(max(level, 0), 8)


def extract_docx(path: Path, images: bool = True) -> dict:
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        from docx.oxml.ns import qn
    except ImportError:
        raise PreviewUnavailable("Document previews need the python-docx package.")
    _guard_archive(path)
    try:
        document = docx.Document(str(path))
    except Exception as e:
        logger.info("docx preview failed for %s: %s", path.name, e)
        raise PreviewUnavailable("This document couldn't be read.")

    blocks, truncated, images = [], False, _ImageBudget(images)
    def append(block):
        nonlocal truncated
        if len(blocks) < MAX_DOC_BLOCKS:
            blocks.append(block)
        else:
            truncated = True
    for child in document.element.body.iterchildren():
        if len(blocks) >= MAX_DOC_BLOCKS:
            truncated = True
            break
        tag = child.tag.split("}")[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            heading = int(style[7:].strip()) if style.startswith("Heading") and style[7:].strip().isdigit() else 0
            list_kind, level = _doc_list(paragraph, document)
            runs, remaining = [], MAX_CELL_CHARS
            def flush():
                if runs:
                    text = "".join(r["text"] for r in runs)
                    append({"type": "heading" if heading else "list" if list_kind else "paragraph",
                            "level": min(heading, 6) if heading else level, "list": list_kind,
                            "text": text, "runs": list(runs)})
                    runs.clear()
            # Walk run children to retain text/picture/text order within a paragraph.
            # Hyperlink runs are included, but external relationships are never fetched.
            from docx.text.run import Run
            for run_element in child.iter(qn("w:r")):
                if truncated:
                    break
                run = Run(run_element, paragraph)
                for element in run_element:
                    if truncated:
                        break
                    tag = element.tag.split("}")[-1]
                    value = (element.text or "") if tag == "t" else "\t" if tag == "tab" else "\n" if tag in ("br", "cr") else ""
                    if value and remaining:
                        value = _clip(value)[:remaining]
                        remaining -= len(value)
                        runs.append({"text": value, "bold": bool(run.bold), "italic": bool(run.italic), "underline": bool(run.underline)})
                    if tag == "drawing":
                        flush()
                        for blip in element.iter(qn("a:blip")):
                            if truncated:
                                break
                            rid = blip.get(qn("r:embed"))
                            uri = None
                            try:
                                if rid:
                                    uri = images.read(document.part.related_parts[rid].blob)
                            except Exception:
                                pass
                            append({"type": "image", "text": "Inline picture" if uri else "Inline picture (unavailable)", "image": uri})
            flush()
        elif tag == "tbl":
            table = Table(child, document)
            rows = [[_clip(cell.text).strip() for cell in row.cells[:MAX_COLS]] for row in table.rows[:MAX_ROWS]]
            if rows:
                append({"type": "table", "rows": rows, "text": _table_text(rows)})
    return {"kind": "docx", "blocks": blocks, "truncated": truncated}


def _ppt_paragraphs(shape):
    from pptx.enum.text import PP_ALIGN
    if not getattr(shape, "has_text_frame", False):
        return []
    paragraphs, remaining = [], MAX_CELL_CHARS
    aligns = {PP_ALIGN.LEFT: "left", PP_ALIGN.CENTER: "center", PP_ALIGN.RIGHT: "right", PP_ALIGN.JUSTIFY: "justify"}
    for paragraph in shape.text_frame.paragraphs:
        if remaining <= 0:
            break
        runs = []
        for run in paragraph.runs:
            text = _clip(run.text)[:remaining]
            remaining -= len(text)
            font = run.font
            runs.append({"text": text, "size_pt": float(font.size.pt) if font.size else None,
                         "bold": bool(font.bold), "italic": bool(font.italic), "underline": bool(font.underline), "color": _rgb(font.color)})
            if remaining <= 0:
                break
        paragraphs.append({"align": aligns.get(paragraph.alignment), "level": min(max(paragraph.level, 0), 8), "runs": runs})
        remaining -= 1
    return paragraphs


def extract_pptx(path: Path, images: bool = True) -> dict:
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except ImportError:
        raise PreviewUnavailable("Slide previews need the python-pptx package.")
    _guard_archive(path)
    try:
        deck = Presentation(str(path))
    except Exception as e:
        logger.info("pptx preview failed for %s: %s", path.name, e)
        raise PreviewUnavailable("This presentation couldn't be read.")

    slides, images = [], _ImageBudget(images)
    for index, slide in enumerate(deck.slides, start=1):
        if index > MAX_SLIDES:
            break
        title, body, tables, shapes, groups = "", [], [], [], []
        try:
            if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                title = _clip(slide.shapes.title.text).strip()
        except Exception:
            pass
        visited = 0
        def walk(collection, transform=(1, 0, 0, 1, 0, 0), group=None, rotation=0, depth=0):
            nonlocal visited
            # Affine transforms compose the OOXML group child coordinate space,
            # including scaling, translation and rotation about the group center.
            a, b, c, d, e, f = transform
            for shape in collection:
                if visited >= MAX_SHAPES_PER_SLIDE or depth > 12:
                    break
                visited += 1
                try:
                    x, y, w, h = map(float, (shape.left, shape.top, shape.width, shape.height))
                    angle = float(shape.rotation)
                    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                        xf = shape._element.grpSpPr.xfrm
                        sx = w / xf.chExt.cx if xf.chExt.cx else 1
                        sy = h / xf.chExt.cy if xf.chExt.cy else 1
                        theta = math.radians(angle)
                        cos, sin = math.cos(theta), math.sin(theta)
                        ga, gb, gc, gd = cos * sx, sin * sx, -sin * sy, cos * sy
                        ge = x + w / 2 - cos * w / 2 + sin * h / 2 - ga * xf.chOff.x - gc * xf.chOff.y
                        gf = y + h / 2 - sin * w / 2 - cos * h / 2 - gb * xf.chOff.x - gd * xf.chOff.y
                        composed = (a*ga+c*gb, b*ga+d*gb, a*gc+c*gd, b*gc+d*gd, a*ge+c*gf+e, b*ge+d*gf+f)
                        start = len(shapes)
                        walk(shape.shapes, composed, shape.shape_id, rotation + angle, depth + 1)
                        groups.append({"id": shape.shape_id, "name": _clip(shape.name), "kind": "group",
                                       "children": [s["id"] for s in shapes[start:]]})
                        continue
                    cx, cy = a*(x+w/2)+c*(y+h/2)+e, b*(x+w/2)+d*(y+h/2)+f
                    aw, ah = w*math.hypot(a,b), h*math.hypot(c,d)
                    kind = "other"
                    # Template photo slots are placeholders that hold a picture.
                    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE or (shape.is_placeholder and hasattr(shape, "image")):
                        kind = "picture"
                    elif shape.has_table:
                        kind = "table"
                    elif shape.has_chart:
                        kind = "chart"
                    elif shape.is_placeholder:
                        kind = "placeholder"
                    elif shape.has_text_frame:
                        kind = "text"
                    item = {"id": shape.shape_id, "name": _clip(shape.name), "kind": kind,
                            "x": round(cx-aw/2), "y": round(cy-ah/2), "w": round(aw), "h": round(ah),
                            "rotation": (angle + rotation) % 360, "z": len(shapes),
                            "fill": _solid_fill(getattr(shape, "fill", None)), "line": None,
                            "paragraphs": _ppt_paragraphs(shape), "rows": [], "image": None}
                    if group is not None:
                        item["group"] = group
                    try:
                        item["line"] = _rgb(shape.line.color)
                    except Exception:
                        pass
                    if kind == "picture":
                        try:
                            item["image"] = images.read(shape.image.blob)
                        except Exception:
                            pass
                    if kind == "table":
                        rows = [[_clip(cell.text).strip() for cell in islice(row.cells, MAX_COLS)] for row in islice(shape.table.rows, MAX_ROWS)]
                        item["rows"] = rows
                        if rows:
                            tables.append(rows)
                    if shape.has_text_frame:
                        text = _clip(shape.text).strip()
                        if text and text != title:
                            body.extend(line.strip() for line in text.split("\n") if line.strip())
                    shapes.append(item)
                except Exception:
                    continue  # one malformed shape must not lose a slide
        walk(slide.shapes)
        notes = ""
        try:
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = _clip(slide.notes_slide.notes_text_frame.text).strip()
        except Exception:
            pass
        slides.append({"index": index, "title": title, "body": body, "tables": tables, "notes": notes,
                       "shapes": shapes, "groups": groups, "background": _solid_fill(slide.background.fill),
                       "truncated_shapes": visited >= MAX_SHAPES_PER_SLIDE})
    return {"kind": "pptx", "slides": slides, "slide_width": int(deck.slide_width), "slide_height": int(deck.slide_height),
            "truncated": len(deck.slides) > MAX_SLIDES, "layout_fidelity": False}


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


def extract(path: Path, extension: str, images: bool = True) -> dict:
    extractor = _EXTRACTORS.get(extension.lower())
    if extractor is None:
        raise PreviewUnavailable("This file type has no structured preview.")
    if extractor in (extract_pptx, extract_docx):
        return extractor(Path(path), images=images)
    return extractor(Path(path))
