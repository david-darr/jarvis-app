"""Files associated with a chat: uploads, generated artifacts and model writes.

Local entries use a root plus relative path saved on the session. A client
receives only an opaque id; preview/download resolves that id against the
session's records and rechecks the path before reading current bytes.
"""
import hashlib
from pathlib import Path
import time
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException

from core.session_manager import session_manager
from core.vault import resolve_vault_dir


def _id(root: str, relative: str) -> str:
    return hashlib.sha256(f"{root}\0{relative}".encode()).hexdigest()[:24]


def _local_record(root: str, relative: str, origin: str, created_at: float | None = None,
                  display_name: str | None = None, internal_paths: list[str] | None = None) -> dict:
    base = Path(root).resolve()
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("invalid chat file path")
    result = {"id": _id(str(base), path.as_posix()), "root": str(base), "path": path.as_posix(),
              "name": display_name or path.name, "origin": origin, "created_at": created_at or time.time()}
    if internal_paths:
        result["internal_paths"] = [Path(name).as_posix() for name in internal_paths]
    return result


def record_local(session_id: str, root: str, relative: str, origin: str,
                 display_name: str | None = None, internal_paths: list[str] | None = None) -> None:
    if origin not in ("attachment", "created"):
        raise ValueError("invalid chat file origin")
    record = _local_record(root, relative, origin, display_name=display_name, internal_paths=internal_paths)
    session_manager.register_chat_file(session_id, record)


def record_checkpoint(event: dict) -> None:
    source = event.get("source", "")
    if not source.startswith("chat:") or event.get("status") != "changed":
        return
    session_id = source[5:]
    if session_manager.get_session(session_id) is None:
        return
    for root in event.get("roots", []):
        created = [change["path"] for change in root.get("changes", [])
                   if change.get("before") is None and change.get("after") is not None]
        created.extend(root.get("created_unprotected", []))
        for name in created:
            # Uploads are copied before the turn snapshot and recorded at
            # send time. Ignore hidden service folders if a model creates
            # one of its own during a turn.
            if any(part.startswith(".") for part in Path(name).parts):
                continue
            record_local(session_id, root["path"], name, "created")


def _records(session: dict) -> list[dict]:
    records = list(session.get("chat_files", []))
    seen = {entry.get("id") for entry in records}
    internal = {name for entry in records for name in entry.get("internal_paths", [])}
    # Older chats have no index. Attachments still in their current working
    # folder remain discoverable without rewriting their session document.
    root = Path(session.get("workspace_dir") or resolve_vault_dir()).resolve()
    folder = root / ".attachments" / session["id"]
    if folder.is_dir() and not folder.is_symlink() and not folder.parent.is_symlink() and folder.resolve().is_relative_to(root):
        for path in folder.iterdir():
            if path.is_file() and not path.is_symlink():
                if path.relative_to(root).as_posix() in internal:
                    continue
                entry = _local_record(str(root), str(path.relative_to(root)), "attachment", path.stat().st_mtime)
                if entry["id"] not in seen:
                    records.append(entry)
                    seen.add(entry["id"])
    return records


def _safe_local(record: dict) -> Path:
    root = Path(record["root"]).resolve()
    relative = Path(record["path"])
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise HTTPException(404, "file is no longer available")
    path = root / relative
    if not path.resolve().is_relative_to(root) or any(part.is_symlink() for part in [path, *path.parents] if part != root):
        raise HTTPException(404, "file is no longer available")
    if not path.is_file():
        raise HTTPException(404, "file is no longer available")
    return path


def resolve_local(session_id: str, file_id: str) -> Path:
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    record = next((r for r in _records(session) if r.get("id") == file_id), None)
    if record is None:
        raise HTTPException(404, "file is not part of this chat")
    return _safe_local(record)


def list_for_session(session_id: str) -> list[dict]:
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    result = []
    for record in _records(session):
        try:
            path = _safe_local(record)
            size, exists = path.stat().st_size, True
        except (HTTPException, OSError, ValueError):
            size, exists = None, False
        result.append({"id": record["id"], "name": record["name"], "origin": record["origin"],
                       "url": f"/chat-files/{record['id']}", "size": size, "exists": exists,
                       "created_at": record.get("created_at")})

    # Generated links in assistant messages are also part of the chat even
    # when an older provider never called register_artifact().
    from core import chat_artifacts
    urls = set(session.get("artifact_urls", []))
    for message in session.get("messages", []):
        if message.get("role") == "assistant":
            urls.update(chat_artifacts.LINK.findall(message.get("content") or ""))
    for url in sorted(urls):
        parsed = urlsplit(url)
        name = Path(unquote(parsed.path)).name
        try:
            _, metadata = chat_artifacts.resolve(session_id, url)
            size, exists = metadata["size"], True
        except HTTPException:
            size, exists = None, False
        result.append({"id": hashlib.sha256(url.encode()).hexdigest()[:24], "name": name,
                       "origin": "generated", "url": url, "size": size, "exists": exists,
                       "created_at": session.get("updated_at")})
    return sorted(result, key=lambda item: item.get("created_at") or 0, reverse=True)


def list_library() -> list[dict]:
    groups = []
    for header in session_manager.list_sessions():
        files = list_for_session(header["id"])
        if files:
            groups.append({"session_id": header["id"], "title": header.get("title") or "Untitled chat",
                           "files": files})
    return groups
