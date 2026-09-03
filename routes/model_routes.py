"""Model endpoint registry — the "bring your own model" CRUD surface (Settings
tab's Models section). Admin-gated like settings_routes.py; encryption of the
API key happens here, at the boundary, same as Discord's token in
settings_routes.py — core/model_endpoints.py itself never sees a plaintext
key from a caller that didn't already go through encrypt().
"""
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import model_endpoints, token_usage
from core.middleware import require_admin
from core.providers import openai_compatible

router = APIRouter(prefix="/api/models", tags=["models"])


class CreateEndpointRequest(BaseModel):
    name: str
    base_url: str = ""
    model: str = ""
    api_key: Optional[str] = None
    kind: str = "api"  # "local", "api", or "claude_cli" — David's ask
    # 2026-08-31, matching Odysseus's Settings > Add Models split between
    # local model servers (Ollama/llama.cpp/vLLM) and hosted API providers,
    # plus a third kind (2026-08-31 follow-up: "the user has to add their
    # own models first... claude code cli" — Claude is no longer a free
    # default, it's just another addable connection). local/api still share
    # one OpenAI-compatible client; claude_cli routes through core/brain.py
    # instead and needs neither base_url nor api_key.
    num_ctx: Optional[int] = None  # local only — see model_endpoints.DEFAULT_LOCAL_NUM_CTX


@router.get("")
async def list_endpoints(user: str = Depends(require_admin)) -> list[dict]:
    return model_endpoints.list_endpoints()


@router.get("/usage")
async def get_usage(user: str = Depends(require_admin)) -> dict:
    """Home tab's "AI Models" card (David's ask 2026-09-01). See
    core/token_usage.py's module docstring — best-effort, percentage is
    share of usage across endpoints that have reported any, not a percentage
    of a fixed budget/cap this app doesn't have."""
    return token_usage.get_usage_summary()


@router.post("")
async def create_endpoint(body: CreateEndpointRequest, user: str = Depends(require_admin)) -> dict:
    try:
        return model_endpoints.create_endpoint(body.name, body.base_url, body.model, body.api_key, body.kind, body.num_ctx)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{endpoint_id}")
async def delete_endpoint(endpoint_id: str, user: str = Depends(require_admin)) -> dict:
    if model_endpoints.get_endpoint(endpoint_id) is None:
        raise HTTPException(status_code=404, detail="endpoint not found")
    model_endpoints.delete_endpoint(endpoint_id)
    return {"ok": True}


@router.post("/{endpoint_id}/test")
async def test_endpoint(endpoint_id: str, user: str = Depends(require_admin)) -> dict:
    """Real reachability probe (David's ask 2026-08-31, matching Odysseus's
    Test/Probe buttons) — sends one minimal chat completion and reports
    whether it actually succeeded, not just whether the URL is well-formed."""
    ep = model_endpoints.get_endpoint(endpoint_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="endpoint not found")
    started = time.monotonic()
    if ep.get("kind") == "claude_cli":
        # No base_url/api_key to probe — the real question is whether the
        # `claude` CLI itself is installed and logged in on this machine, so
        # the honest test is a real minimal turn through the same Brain path
        # a chat would use, not a fake "always ok" response.
        from core.brain import Brain
        brain = Brain(model=ep.get("model") or None)
        try:
            await brain.connect()
            await brain.run_turn("ping")
            return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000)}
        except Exception as e:
            return {"ok": False, "detail": str(e)[:300]}
        finally:
            await brain.disconnect()
    base_url, model, api_key, num_ctx = model_endpoints.resolve_runtime(endpoint_id)
    try:
        await openai_compatible.run_turn(base_url, model, api_key, [{"role": "user", "content": "ping"}], num_ctx=num_ctx)
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000)}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:300]}
