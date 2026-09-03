"""Session CRUD — the sidebar list, create/rename/delete surface."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import workspace
from core.middleware import require_admin, require_user
from core.session_manager import session_manager
from services import chat_service

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    title: str = "New Chat"


class RenameSessionRequest(BaseModel):
    title: str


class StarSessionRequest(BaseModel):
    starred: bool


class SetModelRequest(BaseModel):
    model_endpoint_id: str | None = None


class SetWorkspaceRequest(BaseModel):
    path: str | None = None


class AppendMessageRequest(BaseModel):
    role: str
    content: str


class SetIntegrationsRequest(BaseModel):
    enabled_integration_ids: list[str] | None = None


@router.get("")
async def list_sessions(user: str = Depends(require_user)) -> list[dict]:
    return session_manager.list_sessions()


@router.post("")
async def create_session(body: CreateSessionRequest, user: str = Depends(require_user)) -> dict:
    return session_manager.create_session(body.title)


@router.get("/{session_id}")
async def get_session(session_id: str, user: str = Depends(require_user)) -> dict:
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@router.patch("/{session_id}")
async def rename_session(session_id: str, body: RenameSessionRequest, user: str = Depends(require_user)) -> dict:
    try:
        session_manager.rename_session(session_id, body.title)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True}


@router.post("/{session_id}/star")
async def star_session(session_id: str, body: StarSessionRequest, user: str = Depends(require_user)) -> dict:
    try:
        return session_manager.set_starred(session_id, body.starred)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@router.post("/{session_id}/model")
async def set_session_model(session_id: str, body: SetModelRequest, user: str = Depends(require_user)) -> dict:
    """Pins a session to a registered core/model_endpoints.py entry, or clears
    it back to the default Claude Agent SDK brain. Closes any already-open
    Brain for this session so the next message reconnects with the new model
    — never swap the provider under a live connection mid-turn."""
    try:
        session_manager.set_model_endpoint(session_id, body.model_endpoint_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    await chat_service.close_session_brain(session_id)
    return {"ok": True}


@router.post("/{session_id}/workspace")
async def set_session_workspace(session_id: str, body: SetWorkspaceRequest, user: str = Depends(require_admin)) -> dict:
    """Pins a session's agent tools to a folder (see core/workspace.py), or
    clears back to vault-only scope. Admin-gated: this widens what the
    agent's file/shell tools can reach on the host, same sensitivity as the
    tools themselves. Re-vets server-side even though the client should have
    already called /api/workspace/vet — never trust a client-supplied path.
    Closes any live Brain so the next message reconnects with the new cwd."""
    resolved = workspace.vet_workspace(body.path) if body.path else None
    if body.path and not resolved:
        raise HTTPException(status_code=400, detail="not a usable workspace folder")
    try:
        session_manager.set_workspace(session_id, resolved)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    await chat_service.close_session_brain(session_id)
    return {"workspace_dir": resolved}


@router.post("/{session_id}/integrations")
async def set_session_integrations(session_id: str, body: SetIntegrationsRequest, user: str = Depends(require_user)) -> dict:
    """Restricts which MCP Tool Server integrations this chat can reference
    (David's ask 2026-08-31, matching Claude's per-conversation connector
    toggle) — None means "all registered ones," the pre-existing global
    default. Closes any live Brain so the next message reconnects with the
    new set, same pattern as /model and /workspace above."""
    try:
        session_manager.set_integrations(session_id, body.enabled_integration_ids)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    await chat_service.close_session_brain(session_id)
    return {"ok": True}


@router.post("/{session_id}/messages")
async def append_message(session_id: str, body: AppendMessageRequest, user: str = Depends(require_user)) -> dict:
    """Appends a message to a session's history without invoking the agent —
    for client-side interactions (slash commands, David's ask 2026-08-31)
    that should show up when the chat is reopened, but never went to Claude
    in the first place. Real bug found live: slash-command output was only
    ever appended to the DOM, never persisted, so it vanished on session
    reopen — this route is the fix, not a new feature for its own sake."""
    if body.role not in ("user", "assistant"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'assistant'")
    try:
        session_manager.append_message(session_id, body.role, body.content)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True}


@router.delete("/{session_id}")
async def delete_session(session_id: str, user: str = Depends(require_user)) -> dict:
    await chat_service.close_session_brain(session_id)
    session_manager.delete_session(session_id)
    return {"ok": True}
