"""Calendar CRUD + range listing (merges real events with due-dated Notes)."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.middleware import require_user
from services.calendar_service import calendar_service

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


class CreateEventRequest(BaseModel):
    title: str
    start: str
    end: str
    all_day: bool = False
    location: str = ""
    description: str = ""


class UpdateEventRequest(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    all_day: Optional[bool] = None
    location: Optional[str] = None
    description: Optional[str] = None
    completed: Optional[bool] = None


@router.get("/events")
async def list_events(start: str = Query(...), end: str = Query(...), user: str = Depends(require_user)) -> list[dict]:
    return calendar_service.list_range(start, end)


@router.get("/events/archived")
async def list_archived_events(user: str = Depends(require_user)) -> list[dict]:
    return calendar_service.list_completed()


@router.post("/events")
async def create_event(body: CreateEventRequest, user: str = Depends(require_user)) -> dict:
    return calendar_service.create_event(body.title, body.start, body.end, body.all_day, body.location, body.description)


@router.patch("/events/{event_id}")
async def update_event(event_id: str, body: UpdateEventRequest, user: str = Depends(require_user)) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return calendar_service.update_event(event_id, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail="event not found")


@router.delete("/events/{event_id}")
async def delete_event(event_id: str, user: str = Depends(require_user)) -> dict:
    calendar_service.delete_event(event_id)
    return {"ok": True}
