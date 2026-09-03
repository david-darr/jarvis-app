"""Notes CRUD — the Notes tab."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.middleware import require_user
from services.notes_service import notes_service

router = APIRouter(prefix="/api/notes", tags=["notes"])


class CreateNoteRequest(BaseModel):
    text: str
    due_date: Optional[str] = None
    project: str = "personal"


class UpdateNoteRequest(BaseModel):
    text: Optional[str] = None
    due_date: Optional[str] = None
    project: Optional[str] = None
    completed: Optional[bool] = None


@router.get("")
async def list_notes(include_completed: bool = Query(True), user: str = Depends(require_user)) -> list[dict]:
    return notes_service.list_notes(include_completed)


@router.post("")
async def create_note(body: CreateNoteRequest, user: str = Depends(require_user)) -> dict:
    return notes_service.create_note(body.text, body.due_date, body.project)


@router.patch("/{note_id}")
async def update_note(note_id: str, body: UpdateNoteRequest, user: str = Depends(require_user)) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return notes_service.update_note(note_id, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail="note not found")


@router.delete("/{note_id}")
async def delete_note(note_id: str, user: str = Depends(require_user)) -> dict:
    notes_service.delete_note(note_id)
    return {"ok": True}
