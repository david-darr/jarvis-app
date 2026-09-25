"""Settings > Administration > Sandbox changes (Hermes phase 7, 2026-09-24).

Edits a model made to a sandboxed copy of the JARVIS code wait here as
change sets (core/sandbox_changes.py) until an admin applies or discards
them. Admin-only: applying writes into the app's own code. There is no model
tool behind any of these routes.
"""
from fastapi import APIRouter, Depends, HTTPException

from core import sandbox_changes
from core.middleware import require_admin

router = APIRouter(prefix="/api/sandbox/changes", tags=["sandbox"])


@router.get("")
async def list_changes(user: str = Depends(require_admin)) -> list[dict]:
    return sandbox_changes.list_pending()


@router.get("/{change_id}")
async def get_change(change_id: str, user: str = Depends(require_admin)) -> dict:
    try:
        return sandbox_changes.get(change_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="change set not found (applied, discarded or expired)")


@router.post("/{change_id}/apply")
async def apply_change(change_id: str, user: str = Depends(require_admin)) -> dict:
    try:
        return {"applied": sandbox_changes.apply(change_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="change set not found (applied, discarded or expired)")
    except sandbox_changes.Conflict as e:
        raise HTTPException(status_code=409, detail=f"Nothing was applied. {e}")


@router.delete("/{change_id}")
async def discard_change(change_id: str, user: str = Depends(require_admin)) -> dict:
    try:
        sandbox_changes.discard(change_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="change set not found")
    return {"ok": True}
