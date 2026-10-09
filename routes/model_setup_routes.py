"""Admin-only setup, with polled installation progress and broker approval."""
import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict

from core.middleware import require_admin
from services import model_setup

router = APIRouter(prefix='/api/model-setup', tags=['model-setup'], dependencies=[Depends(require_admin)])


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['claude', 'codex', 'claude_cli', 'codex_cli', 'api']
    provider: Literal['openai', 'anthropic', 'openrouter', 'google'] | None = None
    key: str = Field(default='', max_length=4096)


@router.get('/status')
async def status():
    return await asyncio.to_thread(model_setup.status)


@router.post('/codex/install')
async def install():
    try:
        return model_setup.begin_install()
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.get('/codex/install/{job_id}')
async def progress(job_id: str):
    try:
        return model_setup.install_progress(job_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.post('/codex/install/{job_id}/cancel')
async def cancel(job_id: str):
    try:
        return model_setup.cancel_install(job_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.post('/sign-in/{kind}')
async def sign_in(kind: Literal['claude', 'codex']):
    try:
        return await asyncio.to_thread(model_setup.open_sign_in, kind)
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.get('/sign-in/{kind}/wait')
async def wait(kind: Literal['claude', 'codex'], timeout: float = 120):
    return await model_setup.wait_signed_in(kind, timeout)


@router.post('/api-key/test')
async def test_key(body: ConnectionRequest):
    try:
        return await model_setup.test_api_key(body.provider, body.key)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post('/connections')
async def connection(body: ConnectionRequest):
    try:
        return await model_setup.create_connection(**body.model_dump())
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
