"""Lifecycle hooks (services/hook_service.py): Settings > Administration >
Hooks. Every route is admin-only, and no model tool reaches any of this: a
hook can run a command on this computer, so only the person makes them."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.middleware import require_admin
from services.hook_service import EVENTS, hook_service

router = APIRouter(prefix="/api/hooks", tags=["hooks"])


class HookBody(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    event: Optional[str] = None
    action: Optional[str] = None
    tool_pattern: Optional[str] = None
    agent_id: Optional[str] = None
    source: Optional[str] = None
    config: Optional[dict] = None


class PauseBody(BaseModel):
    paused: bool


@router.get("")
async def list_hooks(user: str = Depends(require_admin)) -> dict:
    return {"hooks": [hook_service.public(h) for h in hook_service.hooks()], "paused": hook_service.paused,
            "events": EVENTS}


@router.post("")
async def create_hook(body: HookBody, user: str = Depends(require_admin)) -> dict:
    fields = body.model_dump(exclude_unset=True)
    try:
        return hook_service.public(hook_service.create(fields.pop("name", ""), **fields))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{hook_id}")
async def update_hook(hook_id: str, body: HookBody, user: str = Depends(require_admin)) -> dict:
    try:
        return hook_service.public(hook_service.update(hook_id, **body.model_dump(exclude_unset=True)))
    except KeyError:
        raise HTTPException(status_code=404, detail="hook not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{hook_id}")
async def delete_hook(hook_id: str, user: str = Depends(require_admin)) -> dict:
    try:
        hook_service.delete(hook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="hook not found")
    return {"ok": True}


@router.post("/{hook_id}/test")
async def test_hook(hook_id: str, user: str = Depends(require_admin)) -> dict:
    try:
        return await hook_service.test(hook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="hook not found")


@router.post("/pause")
async def pause(body: PauseBody, user: str = Depends(require_admin)) -> dict:
    hook_service.set_paused(body.paused)
    return {"paused": hook_service.paused}
