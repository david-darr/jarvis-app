"""Image delivery pipeline for chat and Discord (David's ask 2026-09-10).

Redesigned same day: the first version called Pollinations.ai directly for
free, no-key image generation. David's actual ask was narrower and more
deliberate - image generation should go through the user's own Canva
connector, reached via their real Claude Code session, and nothing else. If
that session has no real Claude Code connection (a local/api model endpoint
- see core/external_brain.py), there is structurally no way to reach it:
those sessions never touch core/brain.py or hive_mind_server.py at all, so
this capability was already Claude-Code-only by construction before this
rewrite, no extra gating needed.

Canva does the actual design/generation and export (its own real MCP
tools, called directly by Claude - see core/brain.py's allowed_tools for
exactly which ones are pre-approved). This module's only job is the last
step: pull the resulting download URL onto disk so the existing
chat/Discord delivery pipeline (unchanged from the Pollinations version -
it never cared how the image was produced) can pick it up.
"""
import os
import shutil
import uuid

import httpx

from core.constants import DATA_DIR

GENERATED_DIR = os.path.join(DATA_DIR, "generated_images")
GENERATED_URL_PREFIX = "/generated-images"

# Real Office/PDF files (David's ask 2026-09-10, after the Canva route kept
# hitting MCP permission walls in non-interactive sessions — Discord, a
# scheduled Task — with no one able to answer the prompt). Own code, not
# vendored from any third-party skill repo (license reasons). Separate
# directory from GENERATED_DIR above rather than merged in: that path is
# specifically for images pulled from a URL (Canva export, Pollinations);
# this one is for files a Bash-run script already wrote to local disk —
# different input shape (a local path, not a URL to fetch), kept apart so
# neither path's tests/behavior needed touching to add the other.
GENERATED_FILES_DIR = os.path.join(DATA_DIR, "generated_files")
GENERATED_FILES_URL_PREFIX = "/generated-files"


def register_generated_file(source_path: str) -> dict:
    """Copies an already-created local file (e.g. a .pptx a skill just
    built via python-pptx) into GENERATED_FILES_DIR so it's servable.
    Returns {"path", "filename", "url"}. Raises FileNotFoundError/OSError on
    a real problem — the calling tool turns that into a plain-text
    explanation, same contract as import_image_from_url below."""
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"no such file: {source_path}")
    os.makedirs(GENERATED_FILES_DIR, exist_ok=True)
    original_name = os.path.basename(source_path)
    filename = f"{uuid.uuid4().hex[:12]}_{original_name}"
    dest = os.path.join(GENERATED_FILES_DIR, filename)
    shutil.copyfile(source_path, dest)
    return {"path": dest, "filename": filename, "url": f"{GENERATED_FILES_URL_PREFIX}/{filename}"}


async def import_image_from_url(url: str) -> dict:
    """Downloads url and saves it under GENERATED_DIR. Returns
    {"path", "filename", "url"}. Raises on a real HTTP failure - the calling
    tool (hive_mind_server.py) turns that into a plain-text explanation
    rather than a crash, same as every other best-effort external call in
    this codebase."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("content-type", "")

    os.makedirs(GENERATED_DIR, exist_ok=True)
    ext = "png" if "png" in content_type else "jpg" if "jpg" in content_type or "jpeg" in content_type else _ext_from_url(url)
    filename = f"{uuid.uuid4().hex[:12]}.{ext}"
    path = os.path.join(GENERATED_DIR, filename)
    with open(path, "wb") as f:
        f.write(content)

    return {"path": path, "filename": filename, "url": f"{GENERATED_URL_PREFIX}/{filename}"}


def _ext_from_url(url: str) -> str:
    tail = url.split("?", 1)[0].rsplit(".", 1)
    ext = tail[-1].lower() if len(tail) > 1 else ""
    return ext if ext in ("png", "jpg", "jpeg", "gif", "webp") else "png"
