"""Session CRUD — the sidebar list, create/rename/delete surface."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from functools import wraps
from typing import Literal

from core import attachments, chat_references, workspace, model_catalog, model_endpoints, permissions
from core.auth import auth_manager
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


class SetPermissionModeRequest(BaseModel):
    mode: Literal["base", "auto"]


class FallbackRequest(BaseModel):
    model_endpoint_id: str
    failed_index: int


class SetWorkspaceRequest(BaseModel):
    path: str | None = None


class AppendMessageRequest(BaseModel):
    role: str
    content: str


class RewindRequest(BaseModel):
    keep: int  # how many of the chat's messages stay; the rest are deleted


class ForkRequest(BaseModel):
    through_index: int  # include this message in the new chat


class SetIntegrationsRequest(BaseModel):
    enabled_integration_ids: list[str] | None = None


class SetProjectRequest(BaseModel):
    project_id: str | None = None


class SetOpenMicRequest(BaseModel):
    active: bool


def _for_client(session: dict) -> dict:
    """A session as the browser gets it. An OpenAI-compatible reply's saved
    `tool_rounds` exist for the model (see core/external_brain.py's _seed),
    not the chat window, and can be large - a whole file read, say - so they
    stay on the server."""
    messages = [{k: v for k, v in m.items() if k != "tool_rounds"} for m in session.get("messages") or []]
    return {**session, "messages": messages} if "messages" in session else session


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
    result = _for_client(session)
    if not auth_manager.is_admin(user):
        result = {**result, "permission_mode": "base"}
    return result


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
        return _for_client(session_manager.set_starred(session_id, body.starred))
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@router.post("/{session_id}/model")
async def set_session_model(session_id: str, body: SetModelRequest, user: str = Depends(require_user)) -> dict:
    """Select a provider and a session-local model variant."""
    endpoint = model_endpoints.get_endpoint(body.model_endpoint_id) if body.model_endpoint_id else None
    if body.model_endpoint_id and endpoint is None:
        raise HTTPException(400, "model endpoint not found")
    override = body.model_override
    if override is not None:
        if not endpoint or endpoint["kind"] not in ("claude_cli", "codex_cli", "api"):
            raise HTTPException(400, "Model versions are available only for CLI and API endpoints")
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


@router.post("/{session_id}/permission-mode")
@idle_session
async def set_session_permission_mode(session_id: str, body: SetPermissionModeRequest,
                                      user: str = Depends(require_user)) -> dict:
    if body.mode == "auto" and not auth_manager.is_admin(user):
        raise HTTPException(403, "Auto mode is available only to admins")
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    if session.get("permission_mode", "base") != body.mode:
        await chat_service.close_session_brain(session_id)
        session_manager.set_permission_mode(session_id, body.mode)
        permissions.record_mode_change(session_id, body.mode, user)
    return {"mode": body.mode}


@router.post("/{session_id}/fallback")
@idle_session
async def retry_with_model(session_id: str, body: FallbackRequest, user: str = Depends(require_user)) -> dict:
    """Explicitly switch providers and remove only a failed empty turn for retry.

    The client resends the saved user message after this returns. Validation
    happens before changing the transcript, so a stale UI or lost attachment
    cannot silently delete the user's last question.
    """
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    endpoint = model_endpoints.get_endpoint(body.model_endpoint_id)
    if endpoint is None:
        raise HTTPException(400, "model endpoint not found")
    messages = session.get("messages", [])
    index = body.failed_index
    if index != len(messages) - 1 or index < 1 or messages[index]["role"] != "assistant" or \
            messages[index].get("status") != "failed" or messages[index - 1]["role"] != "user":
        raise HTTPException(409, "This chat has changed since the failed response. Reopen it before retrying.")
    failed, original = messages[index], messages[index - 1]
    if failed.get("failure_kind") not in ("model_error", "rate_limited"):
        raise HTTPException(409, "This turn is not eligible for model fallback")
    if failed.get("content") or failed.get("failure_had_tools"):
        raise HTTPException(409, "This failed response has partial text or tool activity. Switch models and continue instead.")
    if endpoint["id"] == failed.get("failure_model_endpoint_id"):
        raise HTTPException(400, "Choose a different model for fallback")
    floor = (session.get("compactions") or [{}])[-1].get("through_index", 0)
    if index - 1 < floor:
        raise HTTPException(409, "This message is inside a compacted part of the chat")
    saved_attachments = original.get("attachments") or []
    if not saved_attachments and "[Attached file(s)" in (original.get("sent") or ""):
        raise HTTPException(409, "The original attachments cannot be restored. Reattach them before sending again.")
    if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or
           not attachments.staged_file_info(item["id"])
           for item in saved_attachments):
        raise HTTPException(409, "An original attachment is no longer available. Reattach it before sending again.")
    if original.get("image_attachment_ids") and endpoint["kind"] in ("local", "api") and not endpoint.get("supports_images"):
        raise HTTPException(400, "Choose an image-capable model for this message")
    if original.get("references"):
        try:
            chat_references.resolve(session_id, original["references"])
        except ValueError:
            raise HTTPException(409, "A selected reference is no longer available. Choose it again before sending.")
    await chat_service.close_session_brain(session_id)
    session_manager.rewind(session_id, index - 1)
    session_manager.set_model_endpoint(session_id, endpoint["id"])
    return {"ok": True, "model_endpoint_id": endpoint["id"]}


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


@router.post("/{session_id}/rewind")
@idle_session
async def rewind_session(session_id: str, body: RewindRequest, user: str = Depends(require_user)) -> dict:
    """Edit and regenerate (2026-09-25): cut the chat back to its first
    `keep` messages. Refused while a reply is running (idle_session). The
    live connection is closed so the next turn starts from the trimmed
    transcript; see SessionManager.rewind for what that costs."""
    try:
        session = session_manager.rewind(session_id, body.keep)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await chat_service.close_session_brain(session_id)
    return _for_client(session)


@router.post("/{session_id}/fork")
@idle_session
async def fork_session(session_id: str, body: ForkRequest, user: str = Depends(require_user)) -> dict:
    source = session_manager.get_session(session_id)
    if source is None:
        raise HTTPException(status_code=404, detail="session not found")
    if source.get("workspace_dir"):
        if not auth_manager.is_admin(user):
            raise HTTPException(status_code=403, detail="Only an admin can fork a chat with a custom workspace")
        if workspace.vet_workspace(source["workspace_dir"]) != source["workspace_dir"]:
            raise HTTPException(status_code=400, detail="The source chat's workspace is no longer available")
    try:
        fork = session_manager.fork_session(session_id, body.through_index,
                                            allow_workspace=auth_manager.is_admin(user))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return {"id": fork["id"], "title": fork["title"], "project_id": fork.get("project_id")}


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
