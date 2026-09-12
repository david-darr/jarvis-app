"""Projects (David's ask 2026-09-12, "similar to how Claude and ChatGPT have
projects that contain multiple chats that feed on the same submemory") — a
named container of custom instructions + a set of Library documents, shared
by every chat session assigned to it.

Scoped to match real Claude/ChatGPT Projects behavior, confirmed with David
before building: chats inside a project stay independent conversations (no
cross-chat history sharing) — only the instructions and document set are
shared. Document "knowledge" reuses the existing Library (services/
documents_service.py) rather than a second upload/storage system: a project
just holds a list of existing document ids.

project_addendum() is deliberately a pointer, not a memory dump (same
token-efficiency posture as core/system_prompt.py's landing zone) — it
names the project's instructions and lists its documents by id/title, and
every model already has a working read_document tool (Claude's hive-mind
MCP server, ExternalBrain's function tools, or Codex's hive_mind_cli.py) to
pull one in on demand. Inlining every project document's full content into
every turn would work too, closer to how Claude/ChatGPT's own Projects
behave, but bloats token cost with content the model may not need for a
given message — not worth it when the pull-on-demand path already exists
and works identically across every connected model kind.

JSON-backed, same atomic-write convention as model_endpoints.py.
"""
import os
import time
import uuid
from typing import Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from services import documents_service

PROJECTS_FILE = os.path.join(DATA_DIR, "projects.json")


def _load() -> dict:
    return read_json(PROJECTS_FILE, {})


def _save(data: dict) -> None:
    write_json_atomic(PROJECTS_FILE, data)


def list_projects() -> list[dict]:
    return sorted(_load().values(), key=lambda p: p["name"].lower())


def get_project(project_id: str) -> Optional[dict]:
    return _load().get(project_id)


def create_project(name: str, instructions: str = "") -> dict:
    project_id = uuid.uuid4().hex[:12]
    now = time.time()
    project = {
        "id": project_id, "name": name, "instructions": instructions,
        "document_ids": [], "created_at": now, "updated_at": now,
    }
    data = _load()
    data[project_id] = project
    _save(data)
    return project


def update_project(project_id: str, name: Optional[str] = None, instructions: Optional[str] = None) -> dict:
    data = _load()
    if project_id not in data:
        raise KeyError(f"no such project: {project_id}")
    if name is not None:
        data[project_id]["name"] = name
    if instructions is not None:
        data[project_id]["instructions"] = instructions
    data[project_id]["updated_at"] = time.time()
    _save(data)
    return data[project_id]


def delete_project(project_id: str) -> None:
    data = _load()
    data.pop(project_id, None)
    _save(data)


def add_document(project_id: str, doc_id: str) -> dict:
    data = _load()
    if project_id not in data:
        raise KeyError(f"no such project: {project_id}")
    if doc_id not in data[project_id]["document_ids"]:
        data[project_id]["document_ids"].append(doc_id)
        data[project_id]["updated_at"] = time.time()
        _save(data)
    return data[project_id]


def remove_document(project_id: str, doc_id: str) -> dict:
    data = _load()
    if project_id not in data:
        raise KeyError(f"no such project: {project_id}")
    if doc_id in data[project_id]["document_ids"]:
        data[project_id]["document_ids"].remove(doc_id)
        data[project_id]["updated_at"] = time.time()
        _save(data)
    return data[project_id]


def project_addendum(project_id: Optional[str]) -> str:
    """Empty string for a session with no project (the common case) or a
    project that's since been deleted out from under a session — same
    dangling-reference-treated-as-unset posture as model_endpoint_id."""
    if not project_id:
        return ""
    project = get_project(project_id)
    if project is None:
        return ""
    lines = [f"\n\nThis chat belongs to the project \"{project['name']}\"."]
    if project["instructions"].strip():
        lines.append(f"Project instructions: {project['instructions'].strip()}")
    if project["document_ids"]:
        titles = []
        for doc_id in project["document_ids"]:
            doc = documents_service.get_document(doc_id)
            if doc:
                titles.append(f"[{doc_id}] {doc['title']}")
        if titles:
            lines.append(
                "Project knowledge documents (use your read_document tool with the id shown to read one in full): "
                + "; ".join(titles)
            )
    return "\n".join(lines)
