"""Chat file attachments — David's ask 2026-08-31 ("attach files" in the
composer's overflow menu, matching Odysseus's own attach-strip).

Two-step flow, same shape as any real upload UI: stage a file (returns an id
the composer can show as a removable pill before the message is even sent),
then at send time chat_service copies staged files into the session's active
cwd (vault or workspace — see core/workspace.py) under a per-session
.attachments/ folder, and references the relative path in the message text.
Copying into cwd (rather than leaving them in data/attachments/) is what
makes them actually reachable by the Claude Agent SDK's cwd-scoped file
tools, regardless of whether a workspace is set.
"""
import os
import shutil
import uuid

try:
    import pypdfium2 as pdfium  # BSD-3-Clause/Apache-2.0 — safe to bundle, unlike AGPL PDF libs
except ImportError:
    pdfium = None

from core.constants import DATA_DIR

STAGING_DIR = os.path.join(DATA_DIR, "attachments")

# A scanned PDF's pages are already full-resolution images by construction —
# Claude's own Read tool renders every page and returns them all in one
# response no matter how big services/chat_service.py's transport buffer is
# tuned, which is what produced ATTACHMENT_TOO_LARGE_MESSAGE for an 8.5MB
# scanned course PDF (David's ask 2026-09-13 — see that constant's own
# docstring for the live incident, and the near-identical failure that hit
# a 6-screenshot Discord message before the buffer was raised). A real
# text-based PDF has plenty of extractable text per page; a scan has next
# to none — that's the whole test. Splitting a scanned PDF into one JPEG per
# page before Claude ever sees it means each Read call only returns one
# page, the same escape hatch a person gets by screenshotting pages
# individually, just automatic and without the model having to discover the
# trick itself first.
SCANNED_AVG_CHARS_PER_PAGE = 40
MAX_SPLIT_PAGES = 40
PAGE_RENDER_SCALE = 2.0  # ~144 DPI at PDFium's 72-DPI base — legible, not print-quality
PAGE_JPEG_QUALITY = 78


def _split_scanned_pdf(path: str) -> tuple[list[str], str | None]:
    """If `path` is a scanned/image-based PDF, renders each page to its own
    small JPEG next to it, deletes the original, and returns the new
    filenames. Otherwise leaves the file untouched and returns [its own
    basename]. Second return value is a warning to surface to the user when
    a very long scan got truncated, else None."""
    name = os.path.basename(path)
    if pdfium is None or not name.lower().endswith(".pdf"):
        return [name], None
    try:
        pdf = pdfium.PdfDocument(path)
    except Exception:
        return [name], None  # not a real/parseable PDF — Claude's own tool will report the actual error
    try:
        page_count = len(pdf)
        if page_count == 0:
            return [name], None
        total_chars = sum(pdf[i].get_textpage().count_chars() for i in range(page_count))
        if total_chars / page_count >= SCANNED_AVG_CHARS_PER_PAGE:
            return [name], None  # real text layer — Claude can read it directly, no need to touch it

        stem = os.path.splitext(path)[0]
        split_names = []
        for i in range(min(page_count, MAX_SPLIT_PAGES)):
            image = pdf[i].render(scale=PAGE_RENDER_SCALE).to_pil()
            page_path = f"{stem}_page{i + 1:02d}.jpg"
            image.convert("RGB").save(page_path, "JPEG", quality=PAGE_JPEG_QUALITY)
            split_names.append(os.path.basename(page_path))
    finally:
        pdf.close()

    os.remove(path)
    warning = None
    if page_count > MAX_SPLIT_PAGES:
        warning = f"{name} has {page_count} pages — only the first {MAX_SPLIT_PAGES} were converted to page images"
    return split_names, warning


def _safe_filename(name: str) -> str:
    name = os.path.basename(name).replace("\\", "_").replace("/", "_")
    return name or "file"


def stage_file(filename: str, content: bytes) -> dict:
    """Save an uploaded file to staging, return {id, filename, size}."""
    os.makedirs(STAGING_DIR, exist_ok=True)
    attachment_id = uuid.uuid4().hex[:12]
    safe_name = _safe_filename(filename)
    dest = os.path.join(STAGING_DIR, f"{attachment_id}_{safe_name}")
    with open(dest, "wb") as f:
        f.write(content)
    return {"id": attachment_id, "filename": safe_name, "size": len(content)}


def _find_staged_path(attachment_id: str) -> str | None:
    if not os.path.isdir(STAGING_DIR):
        return None
    prefix = f"{attachment_id}_"
    for name in os.listdir(STAGING_DIR):
        if name.startswith(prefix):
            return os.path.join(STAGING_DIR, name)
    return None


def resolve_for_turn(attachment_ids: list[str], session_id: str, cwd: str) -> tuple[list[str], list[str]]:
    """Copy staged attachments into <cwd>/.attachments/<session_id>/ so the
    agent's cwd-scoped file tools can read them, and return their filenames
    (relative to that folder) for referencing in the message text, plus any
    warnings worth surfacing (e.g. a scanned PDF too long to fully convert —
    see _split_scanned_pdf). Staged originals in data/attachments/ are left
    alone — a turn can be retried without re-uploading."""
    if not attachment_ids:
        return [], []
    dest_dir = os.path.join(cwd, ".attachments", session_id)
    os.makedirs(dest_dir, exist_ok=True)
    names, warnings = [], []
    for attachment_id in attachment_ids:
        src = _find_staged_path(attachment_id)
        if not src:
            continue
        filename = os.path.basename(src).split("_", 1)[1] if "_" in os.path.basename(src) else os.path.basename(src)
        dest = os.path.join(dest_dir, filename)
        shutil.copyfile(src, dest)
        split_names, warning = _split_scanned_pdf(dest)
        names.extend(os.path.join(".attachments", session_id, n) for n in split_names)
        if warning:
            warnings.append(warning)
    return names, warnings
