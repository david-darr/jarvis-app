"""Skills CRUD and scanned community installs for Tool Store."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import auth_manager
from core.middleware import require_admin, require_user
from services import remote_skill_source, skill_curator, skill_recorder, skills_service
from services.agent_service import agent_service

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


class InstallSkillUrlRequest(BaseModel):
    url: str
    confirmed: bool = False
    expected_sha256: str | None = None


class RecordingDraftRequest(BaseModel):
    steps: list[dict]
    agent_name: str | None = None


class RecordedSkillRequest(CreateSkillRequest):
    confirmed: bool = False
    mention_agent_id: str | None = None


@router.get("")
async def list_skills(user: str = Depends(require_user)) -> list[dict]:
    # An unreadable skill is listed with its error and no curation: there is
    # no content to scan or lint (roadmap phase 6).
    return [{**skill, "curation": None if skill.get("error") else skill_curator.describe(skill["slug"])}
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
        # 409 with the scan itself, so Tool Store can show what was found
        # and, for a caution verdict only, offer "Import anyway".
        raise HTTPException(status_code=409, detail={
            "message": "This skill was not imported: its scan found something that needs a look.",
            "needs_confirmation": e.needs_confirmation,
            "report": e.report,
            "findings": e.findings,
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/install-url")
async def install_skill_url(body: InstallSkillUrlRequest, user: str = Depends(require_admin)) -> dict:
    """Install one public GitHub SKILL.md through the existing community scan."""
    try:
        source = await remote_skill_source.fetch_skill(body.url)
        if body.confirmed and source.sha256 != body.expected_sha256:
            raise HTTPException(status_code=409, detail="The skill changed since review. Start the install again.")
        result = skills_service.import_skill("SKILL.md", source.content, confirmed=body.confirmed,
                                             name=source.name, origin=source.source_url, replace=False)
        return {"slug": result["slug"], "description": result["description"],
                "scan": result["scan"], "source_url": source.source_url}
    except skill_curator.SkillImportRefused as e:
        raise HTTPException(status_code=409, detail={
            "message": "This skill needs review before installation.",
            "needs_confirmation": e.needs_confirmation,
            "report": e.report,
            "findings": e.findings,
            "sha256": source.sha256,
        })
    except FileExistsError:
        raise HTTPException(status_code=409, detail="A skill with this name already exists. Remove or rename it first.")
    except remote_skill_source.RemoteSkillError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/recording-draft")
async def recording_draft(body: RecordingDraftRequest, user: str = Depends(require_user)) -> dict:
    if len(body.steps) > 200:
        raise HTTPException(422, "a recording holds at most 200 steps")
    steps = skill_recorder.normalize_steps(body.steps)
    result = skill_recorder.draft(steps, agent_name=body.agent_name)
    return {**result, "steps": steps, "labels": [skill_recorder.step_text(step) for step in steps],
            "content": skills_service._render(result["description"], result["body"])}


@router.post("/from-recording")
async def from_recording(body: RecordedSkillRequest, user: str = Depends(require_user)) -> dict:
    agent = None
    if body.mention_agent_id:
        if not auth_manager.is_admin(user):
            raise HTTPException(403, "admin privileges required to edit an agent")
        agent = agent_service.get(body.mention_agent_id)
        if not agent:
            raise HTTPException(404, "agent not found")
    try:
        result = skills_service.import_skill("SKILL.md", skills_service._render(body.description, body.body),
            name=body.name, confirmed=body.confirmed, source=skill_curator.RECORDED,
            origin="computer demonstration", replace=False)
    except skill_curator.SkillImportRefused as e:
        raise HTTPException(409, detail={"message": "Review this recording's scan before saving.",
            "needs_confirmation": e.needs_confirmation, "report": e.report, "findings": e.findings}) from None
    except FileExistsError:
        raise HTTPException(409, "A skill with this name already exists. Choose another name.") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if agent:
        instructions = (agent.get("instructions") or "").rstrip()
        line = f"Use the '{result['slug']}' skill for the task demonstrated on the computer."
        if line not in instructions.splitlines():
            agent_service.update(agent["id"], instructions=instructions + ("\n" if instructions else "") + line)
    return {**result, "curation": skill_curator.describe(result["slug"])}


@router.get("/{slug}")
async def get_skill(slug: str, user: str = Depends(require_user)) -> dict:
    try:
        skill = skills_service.get_skill(slug)
    except skills_service.SkillUnreadable as e:
        raise HTTPException(status_code=422, detail=f"This skill can't be read: {e}. Delete it, or replace its SKILL.md.")
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
