"""Chat HTTP surface: non-streaming (simple callers) and SSE streaming
(real UI use) endpoints, both session-scoped. Thin adapter over
services/chat_service.py — routes own request/response shape, the service
owns the actual turn logic.
"""
import json

from fastapi import APIRouter, Depends, UploadFile, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from core import attachments, chat_artifacts, office_preview
from core.auth import auth_manager
from core.middleware import require_user
from services import chat_service

router = APIRouter(prefix="/api/chat", tags=["chat"])


class PublishFileRequest(BaseModel):
    session_id: str
    path: str


@router.post("/artifacts")
async def publish_artifact(body: PublishFileRequest, user: str = Depends(require_user)) -> dict:
    return chat_artifacts.publish(body.session_id, body.path)


@router.get("/artifacts")
async def artifact_metadata(session_id: str, url: str, user: str = Depends(require_user)) -> dict:
    _, metadata = chat_artifacts.resolve(session_id, url)
    return metadata


@router.get("/artifacts/office")
async def artifact_office(session_id: str, url: str, user: str = Depends(require_user)) -> dict:
    """Structured contents of a spreadsheet, document, deck, or CSV (David's
    ask 2026-09-15) — sheet grids, reading-order blocks, per-slide text.

    Goes through chat_artifacts.resolve() exactly like every other preview
    route, so the same ownership check applies: a file is readable only if
    this session actually references it. Extraction is local and read-only;
    see core/office_preview.py for the macro/formula/zip-bomb boundaries.
    """
    path, metadata = chat_artifacts.resolve(session_id, url)
    if metadata["kind"] != "office":
        raise HTTPException(400, "This file has no structured preview")
    try:
        return office_preview.extract(path, path.suffix.lower())
    except office_preview.PreviewUnavailable as e:
        # The message is written to be shown to the user as-is, and says why
        # rather than failing blankly.
        raise HTTPException(422, str(e))


@router.get("/artifacts/content")
async def artifact_content(session_id: str, url: str, download: bool = False, user: str = Depends(require_user)):
    path, metadata = chat_artifacts.resolve(session_id, url)
    mime = chat_artifacts.IMAGE_MIMES.get(path.suffix.lower(), "application/pdf" if metadata["kind"] == "pdf" else "text/plain")
    # An Office file served inline would be handed to whatever the browser
    # does with it; the preview reads it through /artifacts/office instead,
    # so the raw bytes are only ever a download.
    if download or metadata["kind"] in ("download", "office"):
        return FileResponse(path, media_type="application/octet-stream", filename=metadata["filename"])
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "no-store"})

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
    if chat_service.session_manager.get_session(body.session_id) is None:
        raise HTTPException(404, "session not found")
    if chat_service.is_busy(body.session_id):
        raise HTTPException(409, "This chat is busy. Wait for the current response to finish.")

    async def event_source():
        try:
            async for item in chat_service.stream_message(body.session_id, body.message, body.attachment_ids, is_admin):
                # A dict is a typed event - today, a permission request raised
                # while the reply was being produced. Text stays exactly as it
                # was, so nothing about the existing protocol changes.
                payload = item if isinstance(item, dict) else {"chunk": item}
                yield f"data: {json.dumps(payload)}\n\n"
            yield "data: {\"done\": true}\n\n"
        except Exception:
            yield f"data: {json.dumps({'error': 'The response was interrupted. Check the selected model and CLI connection, then try again. Any partial reply has been saved.'})}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.post("/attachments")
async def upload_attachment(file: UploadFile, user: str = Depends(require_user)) -> dict:
    content = await file.read(_MAX_ATTACHMENT_BYTES + 1)
    if len(content) > _MAX_ATTACHMENT_BYTES:
        from fastapi import HTTPException
        raise HTTPException(status_code=413, detail="file too large (25MB max)")
    return attachments.stage_file(file.filename or "file", content)
