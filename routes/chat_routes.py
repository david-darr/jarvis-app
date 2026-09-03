"""Chat HTTP surface: non-streaming (simple callers) and SSE streaming
(real UI use) endpoints, both session-scoped. Thin adapter over
services/chat_service.py — routes own request/response shape, the service
owns the actual turn logic.
"""
import json

from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core import attachments
from core.auth import auth_manager
from core.middleware import require_user
from services import chat_service

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Composer-side cap (David's ask 2026-08-31, "attach files" in the overflow
# menu) — generous enough for real documents/screenshots, small enough that
# a mis-click doesn't fill the disk. Matches the order of magnitude other
# attachment paths in this codebase use (voice-line's Discord attachments has
# no cap; this one is deliberately bounded since it's a public-facing upload
# surface, not a trusted-owner Discord DM).
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class ChatRequest(BaseModel):
    session_id: str
    message: str
    attachment_ids: list[str] = []


class ChatResponse(BaseModel):
    reply: str


@router.post("", response_model=ChatResponse)
async def send_chat_message(body: ChatRequest, user: str = Depends(require_user)) -> ChatResponse:
    # Shell execution for connected AI models (David's ask 2026-09-02,
    # modeled on Odysseus's own agent-tool gating — see core/brain.py's
    # _options() and core/external_brain.py) is admin-only; this is the
    # single point that decides that for every turn.
    is_admin = auth_manager.is_admin(user)
    reply = await chat_service.send_message(body.session_id, body.message, body.attachment_ids, is_admin)
    return ChatResponse(reply=reply)


@router.post("/stream")
async def stream_chat_message(body: ChatRequest, user: str = Depends(require_user)) -> StreamingResponse:
    is_admin = auth_manager.is_admin(user)

    async def event_source():
        async for chunk in chat_service.stream_message(body.session_id, body.message, body.attachment_ids, is_admin):
            yield f"data: {json.dumps({'chunk': chunk})}\n\n"
        yield "data: {\"done\": true}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.post("/attachments")
async def upload_attachment(file: UploadFile, user: str = Depends(require_user)) -> dict:
    content = await file.read(_MAX_ATTACHMENT_BYTES + 1)
    if len(content) > _MAX_ATTACHMENT_BYTES:
        from fastapi import HTTPException
        raise HTTPException(status_code=413, detail="file too large (25MB max)")
    return attachments.stage_file(file.filename or "file", content)
