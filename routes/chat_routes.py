"""Chat HTTP surface: non-streaming (simple callers) and SSE streaming
(real UI use) endpoints, both session-scoped. Thin adapter over
services/chat_service.py — routes own request/response shape, the service
owns the actual turn logic.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, UploadFile, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from core import attachments, chat_artifacts, chat_files, chat_references, office_preview
from core.auth import auth_manager
from core.middleware import require_user
from services import chat_service
from services import chat_summary

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.get("/files")
async def list_chat_files(session_id: str, user: str = Depends(require_user)) -> list[dict]:
    return chat_files.list_for_session(session_id)


@router.get("/files/library")
async def list_chat_files_library(user: str = Depends(require_user)) -> list[dict]:
    return chat_files.list_library()


@router.get("/references")
def search_chat_references(q: str = "", session_id: str | None = None,
                           user: str = Depends(require_user)) -> list[dict]:
    return chat_references.search(q, session_id, auth_manager.is_admin(user))


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
    references: list[dict] = []


class ChatResponse(BaseModel):
    reply: str


class ValidateAttachmentsRequest(BaseModel):
    session_id: str
    attachment_ids: list[str]


@router.post("/validate-attachments")
async def validate_attachments(body: ValidateAttachmentsRequest, user: str = Depends(require_user)) -> dict:
    if chat_service.session_manager.get_session(body.session_id) is None:
        raise HTTPException(404, "session not found")
    try:
        chat_service.validate_image_attachments(body.session_id, body.attachment_ids)
    except ValueError as error:
        raise HTTPException(422, str(error))
    return {"ok": True}


@router.post("", response_model=ChatResponse)
async def send_chat_message(body: ChatRequest, user: str = Depends(require_user)) -> ChatResponse:
    # Shell execution for connected AI models (David's ask 2026-09-02,
    # modeled on Odysseus's own agent-tool gating — see core/brain.py's
    # _options() and core/external_brain.py) is admin-only; this is the
    # single point that decides that for every turn.
    is_admin = auth_manager.is_admin(user)
    try:
        chat_service.validate_image_attachments(body.session_id, body.attachment_ids)
        reference_context = await asyncio.to_thread(chat_references.resolve, body.session_id, body.references, is_admin)
    except ValueError as error:
        raise HTTPException(422, str(error))
    reply = await chat_service.send_message(body.session_id, body.message, body.attachment_ids, is_admin,
                                            reference_context, chat_references.metadata(body.references))
    return ChatResponse(reply=reply)


@router.post("/stream")
async def stream_chat_message(body: ChatRequest, user: str = Depends(require_user)) -> StreamingResponse:
    is_admin = auth_manager.is_admin(user)
    if chat_service.session_manager.get_session(body.session_id) is None:
        raise HTTPException(404, "session not found")
    if chat_service.is_busy(body.session_id):
        raise HTTPException(409, "This chat is busy. Wait for the current response to finish.")
    try:
        chat_service.validate_image_attachments(body.session_id, body.attachment_ids)
        reference_context = await asyncio.to_thread(chat_references.resolve, body.session_id, body.references, is_admin)
    except ValueError as error:
        raise HTTPException(422, str(error))

    async def event_source():
        try:
            async for item in chat_service.stream_message(body.session_id, body.message, body.attachment_ids, is_admin,
                                                          reference_context, chat_references.metadata(body.references)):
                # A dict is a typed event - today, a permission request raised
                # while the reply was being produced. Text stays exactly as it
                # was, so nothing about the existing protocol changes.
                payload = item if isinstance(item, dict) else {"chunk": item}
                yield f"data: {json.dumps(payload)}\n\n"
            yield "data: {\"done\": true}\n\n"
        except Exception as error:
            kind = chat_service.failure_kind(error)
            message = ("The selected model is rate limited. Choose another model to retry."
                       if kind == "rate_limited" else
                       "The message could not be completed. Check its attachments and references before retrying."
                       if kind == "request_error" else
                       "The selected model failed. Check its connection or choose another model. Any partial reply has been saved.")
            yield f"data: {json.dumps({'error': message, 'error_kind': kind})}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


class CompactRequest(BaseModel):
    session_id: str


@router.post("/compact")
async def compact_chat(body: CompactRequest, user: str = Depends(require_user)) -> dict:
    return await chat_service.compact_session(body.session_id)


@router.get("/summary")
async def get_chat_summary(session_id: str, user: str = Depends(require_user)) -> dict:
    return chat_summary.get_summary(session_id)


class SummaryRequest(BaseModel):
    session_id: str
    force: bool = False


@router.post("/summary")
async def generate_chat_summary(body: SummaryRequest, user: str = Depends(require_user)) -> dict:
    return await chat_summary.generate_summary(body.session_id, force=body.force)


@router.post("/attachments")
async def upload_attachment(file: UploadFile, user: str = Depends(require_user)) -> dict:
    content = await file.read(_MAX_ATTACHMENT_BYTES + 1)
    if len(content) > _MAX_ATTACHMENT_BYTES:
        from fastapi import HTTPException
        raise HTTPException(status_code=413, detail="file too large (25MB max)")
    return attachments.stage_file(file.filename or "file", content)


@router.get("/images/{session_id}/{attachment_id}")
async def chat_image(session_id: str, attachment_id: str, user: str = Depends(require_user)):
    session = chat_service.session_manager.get_session(session_id)
    if not session or not any(attachment_id in message.get("image_attachment_ids", [])
                              for message in session.get("messages", [])):
        raise HTTPException(404, "image not found in this chat")
    info = attachments.staged_file_info(attachment_id)
    mime = attachments.image_mime(attachment_id)
    if not info or not mime:
        raise HTTPException(404, "image not found")
    return FileResponse(info[0], media_type=mime, headers={"Cache-Control": "private, no-store"})
