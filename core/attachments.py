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

from core.constants import DATA_DIR

STAGING_DIR = os.path.join(DATA_DIR, "attachments")


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


def resolve_for_turn(attachment_ids: list[str], session_id: str, cwd: str) -> list[str]:
    """Copy staged attachments into <cwd>/.attachments/<session_id>/ so the
    agent's cwd-scoped file tools can read them, and return their filenames
    (relative to that folder) for referencing in the message text. Staged
    originals in data/attachments/ are left alone — a turn can be retried
    without re-uploading."""
    if not attachment_ids:
        return []
    dest_dir = os.path.join(cwd, ".attachments", session_id)
    os.makedirs(dest_dir, exist_ok=True)
    names = []
    for attachment_id in attachment_ids:
        src = _find_staged_path(attachment_id)
        if not src:
            continue
        filename = os.path.basename(src).split("_", 1)[1] if "_" in os.path.basename(src) else os.path.basename(src)
        dest = os.path.join(dest_dir, filename)
        shutil.copyfile(src, dest)
        names.append(os.path.join(".attachments", session_id, filename))
    return names
