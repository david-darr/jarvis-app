"""Projects CRUD (David's ask 2026-09-12) — see core/projects.py's module
docstring for the actual design (shared instructions/documents, independent
chat histories)."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import projects
from core.middleware import require_user

router = APIRouter(prefix="/api/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    name: str
    instructions: str = ""


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = None
    instructions: Optional[str] = None


@router.get("")
async def list_projects(user: str = Depends(require_user)) -> list[dict]:
    return projects.list_projects()


@router.post("")
async def create_project(body: CreateProjectRequest, user: str = Depends(require_user)) -> dict:
    return projects.create_project(body.name, body.instructions)


@router.get("/{project_id}")
async def get_project(project_id: str, user: str = Depends(require_user)) -> dict:
    project = projects.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.patch("/{project_id}")
async def update_project(project_id: str, body: UpdateProjectRequest, user: str = Depends(require_user)) -> dict:
    try:
        return projects.update_project(project_id, body.name, body.instructions)
    except KeyError:
        raise HTTPException(status_code=404, detail="project not found")


@router.delete("/{project_id}")
async def delete_project(project_id: str, user: str = Depends(require_user)) -> dict:
    projects.delete_project(project_id)
    return {"ok": True}


@router.post("/{project_id}/documents/{doc_id}")
async def add_document(project_id: str, doc_id: str, user: str = Depends(require_user)) -> dict:
    try:
        return projects.add_document(project_id, doc_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="project not found")


@router.delete("/{project_id}/documents/{doc_id}")
async def remove_document(project_id: str, doc_id: str, user: str = Depends(require_user)) -> dict:
    try:
        return projects.remove_document(project_id, doc_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="project not found")
