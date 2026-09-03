"""Cookbook — browse/pull/manage local models. Two serving backends
(David's ask 2026-09-01, "shouldn't we add something for users that dont
want to download Ollama"): Ollama (core/ollama_client.py, the
`/api/cookbook/*` routes below) and a built-in engine needing no separate
app install at all (core/llamacpp_engine.py, the `/api/cookbook/engine/*`
routes) — real `llama-cpp-python[server]` subprocess over a directly-
downloaded GGUF file, proven end-to-end live before this route file was
written. Both admin-gated like model_routes.py, since this is
Settings-adjacent model management."""
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException

from core import llamacpp_engine, model_endpoints
from core.middleware import require_admin
from services import cookbook_service

router = APIRouter(prefix="/api/cookbook", tags=["cookbook"])


class RegisterRequest(BaseModel):
    name: str
    display_name: str | None = None


@router.get("/status")
async def status(user: str = Depends(require_admin)) -> dict:
    return await cookbook_service.get_status()


@router.get("/catalog")
async def catalog(user: str = Depends(require_admin)) -> list[dict]:
    return cookbook_service.get_catalog()


@router.get("/installed")
async def installed(user: str = Depends(require_admin)) -> list[dict]:
    try:
        return await cookbook_service.get_installed()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Ollama unreachable: {e}")


@router.get("/running")
async def running(user: str = Depends(require_admin)) -> list[dict]:
    try:
        return await cookbook_service.get_running()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Ollama unreachable: {e}")


@router.post("/pull/{name}")
async def pull(name: str, user: str = Depends(require_admin)) -> dict:
    cookbook_service.start_pull(name)
    return {"ok": True}


@router.get("/pull/{name}/status")
async def pull_status(name: str, user: str = Depends(require_admin)) -> dict:
    return cookbook_service.get_pull_progress(name)


@router.delete("/models/{name}")
async def delete_model(name: str, user: str = Depends(require_admin)) -> dict:
    try:
        await cookbook_service.delete_model(name)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Ollama unreachable: {e}")
    return {"ok": True}


@router.post("/register")
async def register(body: RegisterRequest, user: str = Depends(require_admin)) -> dict:
    """One-click "use this model" — creates a Settings > Add Models local
    endpoint pointing at this Ollama model, same as if the user had typed
    it into that form by hand."""
    from core.ollama_client import DEFAULT_BASE_URL
    return model_endpoints.create_endpoint(
        name=body.display_name or body.name,
        base_url=f"{DEFAULT_BASE_URL}/v1",
        model=body.name,
        kind="local",
    )


# -- Built-in engine (no Ollama install needed) --------------------------

@router.get("/engine/catalog")
async def engine_catalog(user: str = Depends(require_admin)) -> list[dict]:
    return llamacpp_engine.CATALOG


@router.get("/engine/downloaded")
async def engine_downloaded(user: str = Depends(require_admin)) -> list[dict]:
    return llamacpp_engine.list_downloaded()


@router.post("/engine/download/{name}")
async def engine_download(name: str, user: str = Depends(require_admin)) -> dict:
    try:
        llamacpp_engine.start_download(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.get("/engine/download/{name}/status")
async def engine_download_status(name: str, user: str = Depends(require_admin)) -> dict:
    return llamacpp_engine.get_download_progress(name)


@router.delete("/engine/models/{name}")
async def engine_delete(name: str, user: str = Depends(require_admin)) -> dict:
    llamacpp_engine.delete_downloaded(name)
    return {"ok": True}


@router.get("/engine/status")
async def engine_status(user: str = Depends(require_admin)) -> dict:
    return llamacpp_engine.status()


@router.post("/engine/start/{name}")
async def engine_start(name: str, user: str = Depends(require_admin)) -> dict:
    try:
        await llamacpp_engine.start(name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return llamacpp_engine.status()


@router.post("/engine/stop")
async def engine_stop(user: str = Depends(require_admin)) -> dict:
    llamacpp_engine.stop()
    return {"ok": True}


@router.post("/engine/register")
async def engine_register(body: RegisterRequest, user: str = Depends(require_admin)) -> dict:
    """Registers the currently-running built-in engine model as a real
    Settings model endpoint — same shape as Ollama's /register above."""
    st = llamacpp_engine.status()
    if not st["running"] or st["model"] != body.name:
        raise HTTPException(status_code=400, detail="that model isn't the one currently running — start it first")
    return model_endpoints.create_endpoint(
        name=body.display_name or body.name,
        base_url=st["base_url"],
        model=body.name,
        kind="local",
    )
