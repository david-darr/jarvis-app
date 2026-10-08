"""Settings > Admin > System — diagnostics, backup export/import, per-domain
wipe (David's ask 2026-08-31, matching Odysseus's diagnostics/backup/
admin_wipe routes). All admin-gated: this surface can read health details
across every domain and destroy data globally.
"""
import os
import shutil
import time
import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from core import backup, custom_tabs, events, logs as log_files, model_endpoints, settings as settings_store, system_admin, task_scheduler
from core.channels import discord_channel
from core.constants import DATA_DIR
from core.middleware import require_admin, require_user
from core.auth import auth_manager
from core.vault import resolve_vault_dir
from services.task_service import task_service

router = APIRouter(prefix="/api/system", tags=["system"])
custom_views_router = APIRouter(tags=["custom-tabs"])


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


def _log_dir() -> str:
    return os.path.join(DATA_DIR, "logs")


@router.get("/logs/files")
async def log_file_list(user: str = Depends(require_admin)) -> list[dict]:
    """The logs Settings > Admin > Logs can show (core/logs.py)."""
    return log_files.list_files(_log_dir())


@router.get("/logs")
async def read_log(name: str = Query("backend"), limit: int = Query(200, ge=1, le=2000),
                   cursor: int | None = Query(None, ge=0), level: str | None = None,
                   tag: str | None = None, since: str | None = None, component: str | None = None,
                   text: str | None = Query(None, max_length=200), user: str = Depends(require_admin)) -> dict:
    """A filtered tail of one log, or, with `cursor`, what was written after
    it - how the Logs view follows a file. Admin-only: logs describe every
    user's activity."""
    try:
        if cursor is None:
            return log_files.tail(name, _log_dir(), limit=limit, level=level, tag=tag, since=since,
                                  component=component, text=text)
        return log_files.follow(name, _log_dir(), cursor, level=level, tag=tag, since=since,
                                component=component, text=text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/custom-tabs")
async def list_custom_tabs(user: str = Depends(require_user)) -> list[dict]:
    """Sidebar manifests for approved user tabs and enabled prebuilt tabs."""
    return custom_tabs.list_manifests()


@router.get("/custom-tabs/pending-approvals")
async def pending_custom_tab_approvals(user: str = Depends(require_admin)) -> list[dict]:
    return custom_tabs.pending_approvals()


@router.get("/tabs")
async def list_tabs(user: str = Depends(require_user)) -> list[dict]:
    tabs = custom_tabs.list_tabs()
    if not auth_manager.is_admin(user):
        for tab in tabs:
            tab.pop("fingerprint", None)
            tab.pop("files", None)
    return tabs


@router.get("/tabs/{slug}/export")
async def export_tab(slug: str, user: str = Depends(require_admin)):
    from core import tab_install
    import asyncio
    try:
        content = await asyncio.to_thread(tab_install.export, slug)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{slug}.kairostab"'})


@router.post("/tabs/install")
async def install_tab(request: Request, user: str = Depends(require_admin)):
    from core import tab_install
    import asyncio
    import zipfile
    try:
        multipart = request.headers.get("content-type", "").startswith("multipart/form-data")
        limit = tab_install.MAX_COMPRESSED + 65536 if multipart else 8192
        incoming = bytearray()
        async for chunk in request.stream():
            incoming.extend(chunk)
            if len(incoming) > limit:
                raise ValueError("Install request exceeds limit")
        # Reconstruct the bounded request for Starlette's multipart parser.
        async def receive():
            return {"type": "http.request", "body": bytes(incoming), "more_body": False}
        bounded = Request(request.scope, receive)
        if multipart:
            async with bounded.form(max_files=1, max_fields=4, max_part_size=tab_install.MAX_COMPRESSED) as form:
                file = form.get("file")
                if not hasattr(file, "read") or not (file.filename or "").lower().endswith(".kairostab"):
                    raise ValueError("Upload a .kairostab file")
                content = await file.read(tab_install.MAX_COMPRESSED + 1)
                options = {"replace": form.get("replace") == "true", "confirmed": form.get("confirmed") == "true",
                           "expected_fingerprint": form.get("expected_fingerprint")}
        else:
            body = await bounded.json()
            if not isinstance(body, dict) or not isinstance(body.get("github_url"), str):
                raise ValueError("Provide a github_url or .kairostab upload")
            content = await asyncio.to_thread(tab_install.github_archive, body["github_url"])
            options = {"replace": body.get("replace") is True, "confirmed": body.get("confirmed") is True,
                       "expected_fingerprint": body.get("expected_fingerprint")}
        result = await asyncio.to_thread(tab_install.install, content, **options)
        from core import tab_hooks
        await tab_hooks.reconcile()
        return result
    except tab_install.ReviewRequired as exc:
        raise HTTPException(status_code=409, detail=exc.detail)
    except (OSError, ValueError, SyntaxError, zipfile.BadZipFile, httpx.HTTPError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class ApproveCustomTabRequest(BaseModel):
    fingerprint: str


@router.post("/custom-tabs/{slug}/approve")
async def approve_custom_tab(slug: str, body: ApproveCustomTabRequest, request: Request,
                             user: str = Depends(require_admin)) -> dict:
    try:
        result = custom_tabs.approve_user_tab(slug, body.fingerprint)
        if os.path.lexists(os.path.join(custom_tabs.USER_TABS_DIR, slug)):
            mounted = custom_tabs.mount_one(request.app, slug)
            from core import tab_hooks
            await tab_hooks.reconcile()
            result["restart_required"] = not mounted
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail="custom tab not found")
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=409, detail=str(e))


@custom_views_router.get("/custom-views/{slug}.js")
async def custom_tab_view(slug: str, user: str = Depends(require_user)):
    result = custom_tabs.approved_view_bytes(slug)
    if result is None:
        raise HTTPException(status_code=404, detail="custom tab view is not approved")
    return Response(content=result, media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})


@custom_views_router.get("/tab-files/{slug}/{filename}")
async def folder_tab_file(slug: str, filename: str, user: str = Depends(require_user)):
    result = custom_tabs.folder_file_bytes(slug, filename)
    if result is None:
        raise HTTPException(status_code=404, detail="tab file is not approved or enabled")
    return Response(content=result, media_type="text/css" if filename == "view.css" else "application/javascript",
                    headers={"Cache-Control": "no-store"})


@router.get("/tab-templates")
async def list_tab_templates(user: str = Depends(require_user)) -> list[dict]:
    """Prebuilt tabs offered in the Tool Store."""
    return custom_tabs.list_templates()


class TabTemplateRequest(BaseModel):
    enabled: bool


@router.post("/tab-templates/{slug}")
async def set_tab_template(slug: str, body: TabTemplateRequest, request: Request,
                           user: str = Depends(require_admin)) -> dict:
    try:
        result = custom_tabs.set_template_enabled(slug, body.enabled)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown tab template")
    # Mount immediately; the loader's dependency guards dormant routes.
    if body.enabled:
        result["restart_required"] = not custom_tabs.mount_one(request.app, slug)
    else:
        result["restart_required"] = slug in getattr(request.app.state, "custom_tab_modules", {})
    from core import tab_hooks
    await tab_hooks.reconcile()
    return result


@router.delete("/custom-tabs/{slug}")
async def delete_custom_tab(slug: str, request: Request, user: str = Depends(require_admin)) -> dict:
    """Remove user source or disable a shipped tab, preserving its data."""
    try:
        result = custom_tabs.delete(slug)
        result["restart_required"] = slug in getattr(request.app.state, "custom_tab_modules", {})
        from core import tab_hooks
        await tab_hooks.reconcile()
        return result
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# Back up everything and restore it (roadmap phase 8, core/backup.py). The
# JSON export below stays: it is portable between machines and versions.
@router.get("/backup/full")
async def full_backup(include_keys: bool = False, user: str = Depends(require_admin)) -> FileResponse:
    """The whole data folder as one zip. Saved passwords and keys only when
    asked for: a backup carrying them is as sensitive as they are."""
    import asyncio
    import tempfile
    folder = tempfile.mkdtemp(prefix="jarvis-full-backup-")
    stamp = time.strftime("%Y-%m-%d-%H%M")
    path = os.path.join(folder, f"jarvis-backup-{stamp}{'-with-keys' if include_keys else ''}.zip")
    await asyncio.to_thread(backup.make_backup, path, include_keys)
    return FileResponse(path, filename=os.path.basename(path), media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, folder, True))


@router.get("/backup/restore")
async def restore_status(user: str = Depends(require_admin)) -> dict:
    return {"pending": backup.pending_restore(), "last": backup.last_restore()}


@router.post("/backup/restore")
async def stage_restore(file: UploadFile, user: str = Depends(require_admin)) -> dict:
    """Check an uploaded backup and swap it in at the next start; the
    current data is kept as a safety copy then."""
    import asyncio
    import tempfile
    folder = tempfile.mkdtemp(prefix="jarvis-restore-upload-")
    try:
        path = os.path.join(folder, "backup.zip")
        with open(path, "wb") as target:
            shutil.copyfileobj(file.file, target)
        manifest = await asyncio.to_thread(backup.stage_restore, path)
    except backup.BackupRefused as e:
        raise HTTPException(status_code=400, detail=f"Not restored: {e}.")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return {"ok": True, "pending": manifest}


@router.delete("/backup/restore")
async def cancel_restore(user: str = Depends(require_admin)) -> dict:
    return {"ok": backup.cancel_restore()}


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
