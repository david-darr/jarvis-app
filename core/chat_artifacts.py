"""Read-only previews of generated files actually referenced by a chat.

No arbitrary filesystem paths or remote URLs. Messages remain the source of
truth, including older sessions: no new artifact database or migration.
"""
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException
from core import image_gen, office_preview
from core.session_manager import session_manager

LINK = re.compile(r"!?\[[^\]]*\]\(<?(/generated-(?:images|files)/[^\s)>]+)>?\)")
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".html", ".htm", ".xml", ".yaml", ".yml", ".sql", ".sh", ".ps1", ".java", ".c", ".cpp", ".rs", ".go", ".log", ".svg"}
IMAGE_MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
# Higher than the text ceiling because Office files carry their own
# compression and embedded media, so a perfectly ordinary deck or workbook is
# routinely bigger than a 2 MB text file. core/office_preview.py applies the
# real limits on how much of it is actually expanded and rendered.
MAX_OFFICE_BYTES = 25 * 1024 * 1024


def resolve(session_id: str, url: str) -> tuple[Path, dict]:
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    allowed = {unquote(match) for m in session.get("messages", []) if m["role"] == "assistant" for match in LINK.findall(m["content"])}
    allowed.update(unquote(item) for item in session.get("artifact_urls", []))
    if unquote(url) not in allowed:
        raise HTTPException(404, "This file is not referenced by this chat")
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise HTTPException(400, "invalid artifact URL")
    decoded = unquote(parsed.path)
    prefix, _, name = decoded.lstrip("/").partition("/")
    if prefix not in ("generated-images", "generated-files") or not name or any(c in name for c in ("/", "\\", ":", "\x00")) or name in (".", ".."):
        raise HTTPException(400, "invalid artifact path")
    root = Path(image_gen.GENERATED_DIR if prefix == "generated-images" else image_gen.GENERATED_FILES_DIR).resolve()
    target = (root / name).resolve()
    if target.parent != root or not target.is_file():
        raise HTTPException(404, "The generated file is no longer available")
    ext = target.suffix.lower()
    size = target.stat().st_size
    # Office formats are checked before the generic text branch: .csv is in
    # TEXT_EXTENSIONS and would otherwise render as raw text rather than a
    # grid. Macro-enabled variants (.xlsm/.docm/.pptm) are deliberately NOT
    # in office_preview.PREVIEWABLE, so they fall through to "download" —
    # these readers cannot execute a macro, but declining to open them at all
    # is a clearer boundary than depending on that.
    if ext in office_preview.PREVIEWABLE:
        kind = "office"
    else:
        kind = "image" if ext in IMAGE_MIMES else "pdf" if ext == ".pdf" else "html" if ext in (".html", ".htm") else "markdown" if ext in (".md", ".markdown") else "text" if ext in TEXT_EXTENSIONS else "download"
    if kind in ("html", "markdown", "text") and size > MAX_PREVIEW_BYTES:
        kind = "download"
    if kind == "office" and size > MAX_OFFICE_BYTES:
        kind = "download"
    return target, {"filename": re.sub(r"^[a-f0-9]{12}_", "", name), "extension": ext.lstrip("."), "size": size, "kind": kind}


def publish(session_id: str, source: str) -> dict:
    """Publish only a file inside the session's already-authorized workspace."""
    from core.vault import resolve_vault_dir
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    root = Path(session.get("workspace_dir") or resolve_vault_dir()).resolve()
    path = (root / source).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(400, "Only files in this chat's workspace can be published")
    if any(part.startswith('.') for part in path.relative_to(root).parts):
        raise HTTPException(400, "Hidden files cannot be published as chat artifacts")
    if path.stat().st_size > 25 * 1024 * 1024:
        raise HTTPException(413, "Generated files must be 25 MB or smaller")
    result = image_gen.register_generated_file(str(path))
    session_manager.register_artifact(session_id, result["url"])
    return {"filename": path.name, "url": result["url"]}
