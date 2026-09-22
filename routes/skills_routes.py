"""Skills CRUD — the Brain tab's skills half."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.middleware import require_admin, require_user
from services import skill_curator, skills_service

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
    # The user's explicit "import anyway" after seeing a caution report.
    # Never overrides a dangerous verdict.
    confirmed: bool = False


@router.get("")
async def list_skills(user: str = Depends(require_user)) -> list[dict]:
    return [{**skill, "curation": skill_curator.describe(skill["slug"])}
            for skill in skills_service.list_skills()]


@router.post("")
async def create_skill(body: CreateSkillRequest, user: str = Depends(require_user)) -> dict:
    try:
        return skills_service.create_skill(body.name, body.description, body.body)
    except (ValueError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/import")
async def import_skill(body: ImportSkillRequest, user: str = Depends(require_user)) -> dict:
    try:
        return skills_service.import_skill(body.filename, body.content, confirmed=body.confirmed)
    except skill_curator.SkillImportRefused as e:
        # 409 with the scan itself, so the Brain tab can show what was found
        # and, for a caution verdict only, offer "Import anyway".
        raise HTTPException(status_code=409, detail={
            "message": "This skill was not imported: its scan found something that needs a look.",
            "needs_confirmation": e.needs_confirmation,
            "report": e.report,
            "findings": e.findings,
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{slug}")
async def get_skill(slug: str, user: str = Depends(require_user)) -> dict:
    try:
        skill = skills_service.get_skill(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return {**skill, "curation": skill_curator.describe(slug)}


@router.post("/{slug}/approve")
async def approve_skill(slug: str, user: str = Depends(require_admin)) -> dict:
    """Let models use a skill whose current content scanned as dangerous.
    Admin-only: skills reach every user's models. Bound to this exact
    content; an edit afterwards needs approving again."""
    try:
        skill_curator.approve(slug)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="skill not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"slug": slug, "curation": skill_curator.describe(slug)}


@router.put("/{slug}")
async def update_skill(slug: str, body: UpdateSkillRequest, user: str = Depends(require_user)) -> dict:
    try:
        return skills_service.update_skill(slug, body.description, body.body)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="skill not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{slug}")
async def delete_skill(slug: str, user: str = Depends(require_user)) -> dict:
    try:
        skills_service.delete_skill(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}
