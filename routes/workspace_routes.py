"""Workspace API — browse server directories to pick a per-session tool
workspace folder. Admin-gated like Odysseus's own version: this enumerates
the server filesystem, the same sensitivity as the file/shell tools
themselves, so it's gated the same way (require_admin).
"""
from fastapi import APIRouter, Depends, Query

from core import workspace
from core.middleware import require_admin

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


@router.get("/browse")
async def browse(path: str = Query(default=""), user: str = Depends(require_admin)) -> dict:
    return workspace.browse_dir(path)


@router.get("/vet")
async def vet(path: str = Query(default=""), user: str = Depends(require_admin)) -> dict:
    resolved = workspace.vet_workspace(path)
    return {"ok": resolved is not None, "path": resolved}
