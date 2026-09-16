"""Session CRUD — the sidebar list, create/rename/delete surface."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from functools import wraps

from core import workspace, model_catalog, model_endpoints
from core.middleware import require_admin, require_user
from core.session_manager import session_manager
from services import chat_service

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def idle_session(fn):
    """Keep transcript/connection mutations atomic relative to live turns."""
    @wraps(fn)
    async def wrapped(session_id: str, *args, **kwargs):
        async with chat_service.session_operation(session_id):
            return await fn(session_id, *args, **kwargs)
    return wrapped


class CreateSessionRequest(BaseModel):
    title: str = "New Chat"


class RenameSessionRequest(BaseModel):
    title: str


class StarSessionRequest(BaseModel):
    starred: bool


class SetModelRequest(BaseModel):
    model_endpoint_id: str | None = None
    model_override: str | None = None
    # None means "send no reasoning effort at all" — the behaviour every
    # session had before this field existed, and the value a client that
    # doesn't know about efforts keeps sending. Validated against
    # core/model_catalog.py below, never passed through blind.
    effort: str | None = None


class SetWorkspaceRequest(BaseModel):
    path: str | None = None


class AppendMessageRequest(BaseModel):
    role: str
    content: str


class SetIntegrationsRequest(BaseModel):
    enabled_integration_ids: list[str] | None = None


class SetProjectRequest(BaseModel):
    project_id: str | None = None


class SetOpenMicRequest(BaseModel):
    active: bool


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
    """Select a provider and (CLI only) a session-local model variant."""
    endpoint = model_endpoints.get_endpoint(body.model_endpoint_id) if body.model_endpoint_id else None
    if body.model_endpoint_id and endpoint is None:
        raise HTTPException(400, "model endpoint not found")
    override = body.model_override
    if override is not None:
        if not endpoint or endpoint["kind"] not in ("claude_cli", "codex_cli"):
            raise HTTPException(400, "Model versions are available only for CLI endpoints")
        override = override.strip()
        import re
        if len(override) > 160 or (override and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+\[\]-]*", override)):
            raise HTTPException(400, "Enter a valid model ID (160 characters maximum)")
    # Reasoning effort (David's ask 2026-09-15). Validated server-side
    # against what the provider actually advertises for the chosen model,
    # because the CLI itself won't catch a bad one: `codex exec
    # --strict-config` rejects unknown config keys but not unknown values,
    # so an invalid effort would reach the provider and fail the turn with a
    # far less useful message. The model being validated against is the one
    # this call is setting, not the session's current one.
    effort = body.effort
    if effort is not None:
        effort = effort.strip() or None
    if effort is not None:
        if not endpoint:
            raise HTTPException(400, "Choose a model before setting a reasoning level")
        resolved_model = (endpoint.get("model") if override is None else override) or None
        if not model_catalog.validate_effort(endpoint["kind"], resolved_model, effort):
            raise HTTPException(400, "That reasoning level isn't supported by the selected model")
    async with chat_service.session_operation(session_id):
        await chat_service.close_session_brain(session_id)
        previous = session_manager.get_session(session_id)
        if endpoint and endpoint["kind"] == "codex_cli" and not (endpoint.get("model") if override is None else override) and previous.get("model_override") != override:
            # A resumed thread may retain its old explicit model. Re-enter
            # through CLI defaults, with saved transcript replay, when the
            # user explicitly resets to an unpinned model.
            session_manager.set_codex_thread_id(session_id, None)
        session_manager.set_model_endpoint(session_id, body.model_endpoint_id, override, effort)
    return {"ok": True, "model_endpoint_id": body.model_endpoint_id, "model_override": override, "effort": effort}


@router.get("/{session_id}/context")
async def get_session_context(session_id: str, user: str = Depends(require_user)) -> dict:
    """This chat's current context occupancy (David's ask 2026-09-15), for
    the meter in the chat header.

    `available: False` is a real, expected answer, not an error — a chat
    that hasn't completed a turn yet, or whose provider reported no usable
    usage, genuinely has nothing to show, and the UI says "unavailable"
    rather than rendering a fabricated percentage. `percent` can also be
    absent on its own when the occupancy is known but the model's capacity
    isn't; the client shows the raw token count in that case.
    """
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    state = session.get("context_state")
    if not state:
        return {"available": False}
    return {"available": True, **state}



@router.post("/{session_id}/open-mic")
@idle_session
async def set_open_mic(session_id: str, body: SetOpenMicRequest, user: str = Depends(require_user)) -> dict:
    """Turn a session into an Open Mic conversation, or end one.

    Ending is the interesting half: the spoken turns were ordinary messages
    while they happened, and they are folded into a summary on the way out so
    the substance survives without the transcript occupying the whole context
    window. See services/chat_service.py's summarise_open_mic for why a fresh
    brain does the summarising.

    Guarded by idle_session like every other session mutation, because
    rewriting the transcript underneath a running turn would race whatever
    that turn is about to append.
    """
    if session_manager.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    if body.active:
        session_manager.set_open_mic(session_id, True)
        return {"open_mic": True}
    result = await chat_service.summarise_open_mic(session_id)
    return {"open_mic": False, **result}

@router.post("/{session_id}/workspace")
@idle_session
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


@router.post("/{session_id}/project")
@idle_session
async def set_session_project(session_id: str, body: SetProjectRequest, user: str = Depends(require_user)) -> dict:
    """Assigns/clears this chat's project (core/projects.py) — its
    instructions/documents are injected into the landing-zone prompt at
    connection time, so any live brain has to reconnect to pick up the
    change, same pattern as /model and /workspace above."""
    try:
        session_manager.set_project(session_id, body.project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    await chat_service.close_session_brain(session_id)
    return {"ok": True}


@router.post("/{session_id}/integrations")
@idle_session
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
@idle_session
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
@idle_session
async def delete_session(session_id: str, user: str = Depends(require_user)) -> dict:
    await chat_service.close_session_brain(session_id)
    session_manager.delete_session(session_id)
    return {"ok": True}
