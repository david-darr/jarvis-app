"""Settings > Channels + task-delivery target list (David's ask 2026-08-31)."""
from fastapi import APIRouter, Depends

from core.channels import registry as channel_registry
from core.middleware import require_user

router = APIRouter(prefix="/api/channels", tags=["channels"])


@router.get("")
async def list_channels(user: str = Depends(require_user)) -> list[dict]:
    return channel_registry.list_channels()
