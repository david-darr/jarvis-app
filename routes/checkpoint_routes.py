"""Admin review and conflict-checked rollback of model file checkpoints."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import file_checkpoints
from core.middleware import require_admin

router = APIRouter(prefix="/api/file-checkpoints", tags=["file-checkpoints"])


class RestoreRequest(BaseModel):
    files: list[dict[str, str]]


@router.get("")
async def list_checkpoints(user: str = Depends(require_admin)) -> list[dict]:
    return file_checkpoints.list_events()


@router.get("/{event_id}")
async def get_checkpoint(event_id: str, user: str = Depends(require_admin)) -> dict:
    try:
        return file_checkpoints.get_event(event_id)
    except KeyError:
        raise HTTPException(404, "checkpoint not found")


@router.post("/{event_id}/restore")
async def restore_checkpoint(event_id: str, request: RestoreRequest,
                             user: str = Depends(require_admin)) -> dict:
    try:
        return {"restored": file_checkpoints.restore(event_id, request.files)}
    except KeyError:
        raise HTTPException(404, "checkpoint not found")
    except file_checkpoints.Conflict as exc:
        raise HTTPException(409, f"Nothing was restored. {exc}")
