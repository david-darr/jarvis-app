"""School tab: Canvas assignments grouped by course, a saved draft per
assignment (the text editor), and a per-course chat session with real
memory — see services/school_service.py for the sync/session-memory logic.
This module just adapts request/response shape, same split as every other
routes/*.py file in this app.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.middleware import require_user
from services.school_service import school_service

router = APIRouter(prefix="/api/tab-school", tags=["school"])

TAB_MANIFEST = {
    "id": "school",
    "label": "School",
    "icon_svg": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M12 3L1 8l11 5 9-4.1V17"/>'
        '<path d="M5 10.5V16c0 1.5 3 3.5 7 3.5s7-2 7-3.5v-5.5"/>'
        "</svg>"
    ),
}


class SettingsRequest(BaseModel):
    canvas_base_url: Optional[str] = None
    canvas_api_token: Optional[str] = None
    ics_url: Optional[str] = None


class CompletedRequest(BaseModel):
    completed: bool


class DraftRequest(BaseModel):
    content: str


@router.get("/settings")
async def get_settings(user: str = Depends(require_user)) -> dict:
    return school_service.get_settings()


@router.put("/settings")
async def update_settings(body: SettingsRequest, user: str = Depends(require_user)) -> dict:
    return school_service.update_settings(body.canvas_base_url, body.canvas_api_token, body.ics_url)


@router.post("/sync")
async def sync(user: str = Depends(require_user)) -> dict:
    try:
        return await school_service.sync()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"sync failed: {e}")


@router.get("/courses")
async def list_courses(user: str = Depends(require_user)) -> list[dict]:
    return school_service.list_courses()


@router.get("/courses/session")
async def get_course_session(course: str, user: str = Depends(require_user)) -> dict:
    """Stable per-course chat session id (created on first call) — the
    School tab's chat panel opens this via /api/sessions/{id} and
    /api/chat/stream directly, same as any normal chat session.

    `course` is a query param, not a path segment (David's report
    2026-09-02: the CS-405 course chat card came up blank) — Canvas course
    names can contain literal "/" (e.g. "CS-405-001/002/009/011", a combined
    section listing), and ASGI servers decode %2F in the path before
    Starlette's router matches it, so a single-segment `{course}` path
    param 404s on any course name a client had to encodeURIComponent(). The
    `?course=` matches every other course-scoped query in this file."""
    return {"session_id": school_service.get_course_session_id(course)}


@router.get("/assignments")
async def list_assignments(course: Optional[str] = None, upcoming_days: Optional[int] = None,
                            overdue: bool = False, include_completed: bool = False,
                            user: str = Depends(require_user)) -> list[dict]:
    return school_service.list_assignments(course, upcoming_days, overdue, include_completed)


@router.get("/assignments/{assignment_id}")
async def get_assignment(assignment_id: str, user: str = Depends(require_user)) -> dict:
    a = school_service.get_assignment(assignment_id)
    if a is None:
        raise HTTPException(status_code=404, detail="assignment not found")
    return a


@router.patch("/assignments/{assignment_id}")
async def update_assignment(assignment_id: str, body: CompletedRequest, user: str = Depends(require_user)) -> dict:
    try:
        return school_service.set_completed(assignment_id, body.completed)
    except KeyError:
        raise HTTPException(status_code=404, detail="assignment not found")


@router.get("/assignments/{assignment_id}/draft")
async def get_draft(assignment_id: str, user: str = Depends(require_user)) -> dict:
    return {"content": school_service.get_draft(assignment_id)}


@router.put("/assignments/{assignment_id}/draft")
async def save_draft(assignment_id: str, body: DraftRequest, user: str = Depends(require_user)) -> dict:
    return school_service.save_draft(assignment_id, body.content)


@router.post("/assignments/{assignment_id}/sync-memory")
async def sync_memory(assignment_id: str, user: str = Depends(require_user)) -> dict:
    """Pushes a context note (assignment details + current draft) into that
    assignment's course chat session — no model turn spent, see
    school_service.sync_course_memory's docstring. Frontend calls this right
    before opening the chat panel for an assignment."""
    result = school_service.sync_course_memory(assignment_id)
    if result is None:
        raise HTTPException(status_code=404, detail="assignment not found")
    return result
