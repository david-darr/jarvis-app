"""Skills CRUD — the Brain tab's skills half."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.middleware import require_user
from services import skills_service

router = APIRouter(prefix="/api/skills", tags=["skills"])


class CreateSkillRequest(BaseModel):
    name: str
    description: str = ""
    body: str = ""


class UpdateSkillRequest(BaseModel):
    description: str = ""
    body: str = ""


class ImportSkillRequest(BaseModel):
    filename: str
    content: str


@router.get("")
async def list_skills(user: str = Depends(require_user)) -> list[dict]:
    return skills_service.list_skills()


@router.post("")
async def create_skill(body: CreateSkillRequest, user: str = Depends(require_user)) -> dict:
    try:
        return skills_service.create_skill(body.name, body.description, body.body)
    except (ValueError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/import")
async def import_skill(body: ImportSkillRequest, user: str = Depends(require_user)) -> dict:
    try:
        return skills_service.import_skill(body.filename, body.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{slug}")
async def get_skill(slug: str, user: str = Depends(require_user)) -> dict:
    skill = skills_service.get_skill(slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return skill


@router.put("/{slug}")
async def update_skill(slug: str, body: UpdateSkillRequest, user: str = Depends(require_user)) -> dict:
    try:
        return skills_service.update_skill(slug, body.description, body.body)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="skill not found")


@router.delete("/{slug}")
async def delete_skill(slug: str, user: str = Depends(require_user)) -> dict:
    skills_service.delete_skill(slug)
    return {"ok": True}
