"""Search and resolve explicit composer references to chat files, vault notes and chats."""
import json
from pathlib import Path

from fastapi import HTTPException

from core import attachments, chat_artifacts, chat_files, memory_tools, office_preview
from core.session_manager import session_manager
from core.untrusted import wrap_untrusted

MAX_REFERENCES = 5
MAX_ITEM_CHARS = 8_000
MAX_TOTAL_CHARS = 24_000
TEXT_EXTENSIONS = chat_artifacts.TEXT_EXTENSIONS


def metadata(references: list[dict]) -> list[dict]:
    """Save only the fields needed to reselect a reference on edit/regenerate."""
    return [{"kind": ref["kind"], "id": ref["id"], "session_id": ref.get("session_id"),
             "label": (ref.get("label") if isinstance(ref.get("label"), str) else "Reference")[:500]}
            for ref in references]


def search(query: str, current_session_id: str | None = None) -> list[dict]:
    """Return compact, client-safe search results; no paths outside the vault."""
    query = query.strip()[:100]
    needle = query.casefold()
    results: list[dict] = []
    for group in chat_files.list_library():
        for item in group["files"]:
            if not item["exists"] or (needle and needle not in (item["name"] + " " + group["title"]).casefold()):
                continue
            results.append({"kind": "file", "id": item["id"], "session_id": group["session_id"],
                            "label": item["name"], "detail": group["title"]})
            if len(results) >= 4:
                break
        if len(results) >= 4:
            break

    if query:
        for hit in memory_tools.search_vault(query, max_results=4):
            path = hit["path"]
            results.append({"kind": "note", "id": path, "label": Path(path).stem, "detail": path})

    headers = session_manager.list_sessions()
    matched = [h for h in headers if h["id"] != current_session_id and
               (not needle or needle in (h.get("title") or "").casefold())]
    if needle and len(matched) < 4:
        ids = {h["id"] for h in matched}
        for hit in memory_tools.search_sessions(query, exclude_session_id=current_session_id, max_results=12):
            if hit["session_id"] not in ids:
                matched.append({"id": hit["session_id"], "title": hit["session_title"]})
                ids.add(hit["session_id"])
    for header in matched[:4]:
        results.append({"kind": "chat", "id": header["id"],
                        "label": header.get("title") or "Untitled chat", "detail": "Conversation"})
    return results


def _file_content(reference: dict) -> tuple[str, str]:
    session_id, file_id = reference.get("session_id"), reference.get("id")
    if not isinstance(session_id, str) or not isinstance(file_id, str):
        raise ValueError("Invalid file reference")
    item = next((entry for entry in chat_files.list_for_session(session_id) if entry["id"] == file_id), None)
    if item is None or not item["exists"]:
        raise ValueError("A referenced file is no longer available")
    url = item["url"]
    if url.startswith("/chat-files/"):
        path = chat_files.resolve_local(session_id, file_id)
    else:
        path, _ = chat_artifacts.resolve(session_id, url)
    label = "selected chat file"
    provenance = f"File: {item['name']}\nFrom chat: {session_id}\n"
    if path.suffix.lower() in office_preview.PREVIEWABLE and path.stat().st_size <= chat_artifacts.MAX_OFFICE_BYTES:
        try:
            content = json.dumps(office_preview.extract(path, path.suffix.lower()), ensure_ascii=False)
            return label, provenance + content[:MAX_ITEM_CHARS]
        except (office_preview.PreviewUnavailable, OSError, ValueError):
            pass
    if path.suffix.lower() == ".pdf" and attachments.pdfium is not None and path.stat().st_size <= chat_artifacts.MAX_OFFICE_BYTES:
        try:
            pdf = attachments.pdfium.PdfDocument(str(path))
            try:
                pages = []
                for page_index in range(min(len(pdf), 30)):
                    pages.append(pdf[page_index].get_textpage().get_text_range())
                    if sum(map(len, pages)) >= MAX_ITEM_CHARS:
                        break
                content = "\n\n".join(pages)[:MAX_ITEM_CHARS]
                if content.strip():
                    return label, provenance + content
            finally:
                pdf.close()
        except Exception:
            pass
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return label, provenance + f"Path: {path}\nNo text could be extracted. Use an available file tool to inspect it if needed."
    with path.open("rb") as handle:
        data = handle.read(MAX_ITEM_CHARS * 4 + 1)
    content = data.decode("utf-8", errors="replace")[:MAX_ITEM_CHARS]
    if len(data) > MAX_ITEM_CHARS or path.stat().st_size > len(data):
        content += "\n[File excerpt truncated.]"
    return label, provenance + content


def _note_content(reference: dict) -> tuple[str, str]:
    path = reference.get("id")
    if not isinstance(path, str) or len(path) > 500:
        raise ValueError("Invalid note reference")
    relative = Path(path)
    if relative.is_absolute() or relative.suffix.lower() != ".md" or any(part in ("", ".", "..") or part.startswith(".") for part in relative.parts):
        raise ValueError("Invalid note reference")
    try:
        content = memory_tools.read_vault_file(path, max_chars=MAX_ITEM_CHARS)
    except (OSError, ValueError) as error:
        raise ValueError("A referenced note is no longer available") from error
    return "selected vault note", f"Note: {path}\n{content}"


def _chat_content(reference: dict, current_session_id: str) -> tuple[str, str]:
    session_id = reference.get("id")
    if not isinstance(session_id, str) or session_id == current_session_id:
        raise ValueError("Invalid chat reference")
    session = session_manager.get_session(session_id)
    if session is None:
        raise ValueError("A referenced chat is no longer available")
    lines = []
    size = 0
    for message in reversed(session.get("messages", [])):
        if message.get("role") not in ("user", "assistant"):
            continue
        line = f"{message['role']}: {message.get('content') or ''}"
        lines.append(line[-MAX_ITEM_CHARS:])
        size += len(lines[-1]) + 2
        if size >= MAX_ITEM_CHARS:
            break
    content = "\n\n".join(reversed(lines))[-MAX_ITEM_CHARS:]
    if size >= MAX_ITEM_CHARS:
        content = "[Earlier messages omitted.]\n" + content
    return "selected chat", f"Chat: {session.get('title') or session_id}\n{content or '[This chat has no messages.]'}"


def resolve(session_id: str, references: list[dict] | None) -> str:
    """Validate client IDs against current records and build bounded model context."""
    if not references:
        return ""
    if len(references) > MAX_REFERENCES:
        raise ValueError(f"Select at most {MAX_REFERENCES} references")
    parts = []
    seen = set()
    total = 0
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError("Invalid reference")
        kind = reference.get("kind")
        if kind not in ("file", "note", "chat"):
            raise ValueError("Invalid reference type")
        ref_id = reference.get("id")
        source_session = reference.get("session_id")
        if (not isinstance(ref_id, str) or len(ref_id) > 500 or
                (source_session is not None and (not isinstance(source_session, str) or len(source_session) > 100))):
            raise ValueError("Invalid reference")
        key = (kind, source_session, ref_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            if kind == "file":
                label, content = _file_content(reference)
            elif kind == "note":
                label, content = _note_content(reference)
            elif kind == "chat":
                label, content = _chat_content(reference, session_id)
        except (HTTPException, OSError) as error:
            raise ValueError("A reference is no longer available") from error
        remaining = MAX_TOTAL_CHARS - total
        if remaining <= 300:
            break
        wrapped = wrap_untrusted(label, content[:min(MAX_ITEM_CHARS, remaining - 300)])
        parts.append(wrapped)
        total += len(wrapped)
    return "\n\n[Selected references for this message]\n" + "\n\n".join(parts) if parts else ""
