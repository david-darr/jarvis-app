"""Admin-only community catalog, review-gated install and revocation controls."""
import zipfile
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from core import tab_install
from services import skill_curator
from services.store_catalog import store_catalog

router = APIRouter(prefix="/api/store", tags=["store"])


class InstallRequest(BaseModel):
    kind: str
    slug: str
    confirmed: bool = False
    expected_fingerprint: str | None = None
    expected_sha256: str | None = None
    expected_commit: str | None = None
    replace: bool = False


class ReenableRequest(BaseModel):
    confirmed: bool = False
    expected_revocation: str | None = None


async def _answer(call):
    try:
        return await call
    except tab_install.ReviewRequired as exc:
        raise HTTPException(409, detail=exc.detail) from None
    except skill_curator.SkillImportRefused as exc:
        raise HTTPException(409, detail={"needs_confirmation": exc.needs_confirmation,
            "report": exc.report, "findings": exc.findings, "sha256": exc.sha256, "commit": exc.commit}) from None
    except (ValueError, OSError, SyntaxError, zipfile.BadZipFile, httpx.HTTPError) as exc:
        raise HTTPException(400, detail=str(exc)) from None


@router.get("/catalog")
async def catalog(user: str = Depends(require_admin)):
    return await _answer(store_catalog.catalog())


@router.post("/refresh")
async def refresh(user: str = Depends(require_admin)):
    return await _answer(store_catalog.catalog(refresh=True))


@router.post("/install")
async def install(body: InstallRequest, user: str = Depends(require_admin)):
    return await _answer(store_catalog.install(**body.model_dump()))


@router.post("/revoked/{kind}/{slug}/reenable")
async def reenable(kind: str, slug: str, body: ReenableRequest, request: Request, user: str = Depends(require_admin)):
    result = await _answer(store_catalog.reenable(kind, slug, **body.model_dump()))
    if kind == "tab":
        from core import custom_tabs, tab_hooks
        result["restart_required"] = not custom_tabs.mount_one(request.app, result["local_id"])
        await tab_hooks.reconcile()
    return result


@router.delete("/installed/{kind}/{slug}")
async def remove(kind: str, slug: str, user: str = Depends(require_admin)):
    return await _answer(store_catalog.remove(kind, slug))
