"""Settings > Admin > System — diagnostics, backup export/import, per-domain
wipe (David's ask 2026-08-31, matching Odysseus's diagnostics/backup/
admin_wipe routes). All admin-gated: this surface can read health details
across every domain and destroy data globally.
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core import custom_tabs, events, model_endpoints, settings as settings_store, system_admin, task_scheduler
from core.channels import discord_channel
from core.middleware import require_admin, require_user
from core.vault import resolve_vault_dir
from services.task_service import task_service

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/status")
async def status(user: str = Depends(require_user)) -> dict:
    """Live mission-control health strip (David's ask 2026-09-02) — cheap,
    in-memory/local-file reads only, no network calls, so Home can poll it
    freely. Distinct from /diagnostics: that's a static admin snapshot; this
    is live state (is the scheduler loop actually alive, which Discord bots
    have a gateway session up right now, what fires next)."""
    enabled_tasks = [t for t in task_service.list_tasks() if t["enabled"] and t["next_run_at"]]
    next_task = min(enabled_tasks, key=lambda t: t["next_run_at"], default=None)
    vault_dir = resolve_vault_dir()
    return {
        "scheduler_running": task_scheduler.is_running(),
        "next_task": {"name": next_task["name"], "next_run_at": next_task["next_run_at"]} if next_task else None,
        "enabled_task_count": len(enabled_tasks),
        "discord_connected_bots": discord_channel.connected_bots(),
        "model_endpoint_count": len(model_endpoints.list_endpoints()),
        "vault_ok": os.path.isdir(vault_dir),
    }


@router.get("/events")
async def recent_events(limit: int = Query(50, ge=1, le=300), user: str = Depends(require_user)) -> list[dict]:
    """Activity feed (core/events.py's ring buffer), newest first."""
    return events.recent(limit)


@router.get("/diagnostics")
async def diagnostics(user: str = Depends(require_admin)) -> dict:
    return system_admin.diagnostics()


@router.get("/custom-tabs")
async def list_custom_tabs(user: str = Depends(require_user)) -> list[dict]:
    """Developer Mode (David's ask 2026-09-01) — nav entries for every
    discovered routes/tab_*.py. require_user, not require_admin, unlike the
    rest of this file: every user needs this to render the sidebar, it's
    not a diagnostic/admin surface."""
    return custom_tabs.list_manifests()


class CustomTabOrderRequest(BaseModel):
    order: list[str]


@router.post("/custom-tabs/order")
async def set_custom_tab_order(body: CustomTabOrderRequest, user: str = Depends(require_admin)) -> dict:
    """Settings > Admin > Custom Tabs reorder (David's ask 2026-09-01)."""
    settings_store.update_settings(custom_tab_order=body.order)
    return {"ok": True}


@router.delete("/custom-tabs/{slug}")
async def delete_custom_tab(slug: str, user: str = Depends(require_admin)) -> dict:
    """Settings > Admin > Custom Tabs delete (David's ask 2026-09-01) — see
    core.custom_tabs.delete()'s docstring for the "still needs a restart to
    fully unmount any of its own API routes" caveat."""
    return custom_tabs.delete(slug)


@router.get("/backup/export")
async def export_backup(user: str = Depends(require_admin)) -> dict:
    return system_admin.export_backup()


class ImportBackupRequest(BaseModel):
    data: dict


@router.post("/backup/import")
async def import_backup(body: ImportBackupRequest, user: str = Depends(require_admin)) -> dict:
    if not isinstance(body.data, dict) or "version" not in body.data:
        raise HTTPException(status_code=400, detail="not a recognized backup file")
    return system_admin.import_backup(body.data)


class WipeRequest(BaseModel):
    kind: str


@router.post("/wipe")
async def wipe(body: WipeRequest, user: str = Depends(require_admin)) -> dict:
    """Client-side double confirmation (Settings tab) is UI protection, not
    the real gate — this admin-only route + the kind allowlist inside
    core.system_admin.wipe() is the actual authorization boundary."""
    try:
        system_admin.wipe(body.kind)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.get("/wipe-kinds")
async def wipe_kinds(user: str = Depends(require_admin)) -> list[str]:
    return sorted(system_admin.WIPE_KINDS)
