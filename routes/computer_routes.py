"""Human view and control of contained computers.

These routes never send human input through the model tool or its audit. The
frame stream refreshes the browser only while someone is watching it.
"""
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from core import computer, computer_image, sandbox, session_manager_store as store
from core.auth import auth_manager
from core.middleware import get_current_user, require_admin, require_user
from core.session_manager_store import get_session_header
from services.agent_service import agent_service

router = APIRouter(prefix="/api/computer", tags=["computer"])


def _access(owner: str, user: str) -> None:
    kind, separator, ident = owner.partition(":")
    if not separator or not ident or kind not in ("chat", "agent"):
        raise HTTPException(404, "computer not found")
    if kind == "agent":
        if not auth_manager.is_admin(user):
            raise HTTPException(403, "admin privileges required")
        if agent_service.get(ident) is None:
            raise HTTPException(404, "agent not found")
        return
    header = get_session_header(ident)
    if header is None:
        raise HTTPException(404, "chat not found")
    if not auth_manager.is_admin(user) and header[0].get("owner_user") != user:
        raise HTTPException(403, "this is another person's chat")


def _running(owner: str) -> None:
    if not computer.manager.is_running(owner):
        raise HTTPException(404, "computer is closed")


@router.get("")
async def list_computers(user: str = Depends(require_user)) -> list[dict]:
    visible = []
    for item in computer.running():
        try:
            _access(item["owner"], user)
        except HTTPException:
            continue
        visible.append(item)
    return visible


@router.get("/status")
async def status(user: str = Depends(require_admin)) -> dict:
    available, reason = await sandbox.available()
    profiles = [{"id": agent["id"], "name": agent["name"], "running": computer.manager.is_running("agent:" + agent["id"])}
                for agent in agent_service.list_agents() if (computer.PROFILES / agent["id"]).is_dir()]
    return {"docker_available": available, "docker_reason": reason,
            "image_ready": await computer_image.image_ready() if available else False,
            "profiles": profiles}


@router.get("/{owner}/frames")
async def frames(owner: str, request: Request, user: str = Depends(require_user)):
    _access(owner, user)
    _running(owner)

    async def stream():
        # Frames are pushed as the page changes (core/computer.py watch()).
        # Remote Access is its own HTTPS server, so plain http from a loopback
        # address is this computer (as core/middleware.py's loopback rule).
        server = request.scope.get("server") or ("", 0)
        local = request.scope.get("scheme") == "http" and server[0] in ("127.0.0.1", "::1")
        frames = computer.manager.watch(owner, local=local)
        try:
            async for frame in frames:
                if await request.is_disconnected() or get_current_user(request) != user:
                    return
                yield "event: frame\ndata: " + json.dumps(frame) + "\n\n"
        except (sandbox.SandboxUnavailable, ValueError):
            pass
        finally:
            await frames.aclose()
        if not await request.is_disconnected():
            yield "event: closed\ndata: {}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/{owner}/last")
async def last(owner: str, user: str = Depends(require_user)):
    _access(owner, user)
    frame = computer.manager.last(owner)
    if frame is None:
        raise HTTPException(404, "no retained computer frame")
    return JSONResponse(frame, headers={"Cache-Control": "no-store"})


@router.get("/{owner}/history")
async def history(owner: str, run_id: str, user: str = Depends(require_user)) -> dict:
    _access(owner, user)
    run = store.get_run(run_id)
    if not owner.startswith("chat:") or not run or run.get("session_id") != owner[5:]:
        raise HTTPException(404, "run not found in this chat")
    return {"steps": [step for step in store.run_events(run_id) if step.get("name") == "computer"]}


@router.post("/{owner}/stop")
async def stop(owner: str, user: str = Depends(require_user)) -> dict:
    _access(owner, user)
    _running(owner)
    await computer.stop(owner)
    return {"ok": True}


@router.post("/{owner}/takeover")
async def takeover(owner: str, user: str = Depends(require_user)) -> dict:
    _access(owner, user)
    _running(owner)
    if not computer.manager.takeover(owner):
        raise HTTPException(404, "computer is closed")
    return {"ok": True}


@router.post("/{owner}/handback")
async def handback(owner: str, user: str = Depends(require_user)) -> dict:
    _access(owner, user)
    _running(owner)
    if not computer.manager.hand_back(owner):
        raise HTTPException(404, "computer is closed")
    return {"ok": True}


class PersonInput(BaseModel):
    kind: Literal["click", "type", "key", "scroll"]
    x: int | None = None
    y: int | None = None
    text: str | None = None
    key: str | None = None
    dx: int | None = None
    dy: int | None = None


@router.post("/{owner}/input")
async def person_input(owner: str, body: PersonInput, user: str = Depends(require_user)) -> dict:
    _access(owner, user)
    _running(owner)
    if body.kind == "click":
        if body.x is None or body.y is None or not 0 <= body.x < 1280 or not 0 <= body.y < 800:
            raise HTTPException(422, "choose a point in the computer frame")
        values = {"x": body.x, "y": body.y}
    elif body.kind == "type":
        if body.text is None or len(body.text) > 10000:
            raise HTTPException(422, "give text to type")
        values = {"text": body.text}
    elif body.kind == "key":
        if not body.key or len(body.key) > 80:
            raise HTTPException(422, "give a key")
        values = {"key": body.key}
    else:
        values = {"dx": body.dx or 0, "dy": body.dy or 0}
    try:
        await computer.manager.person_input(owner, body.kind, values)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return {"ok": True}
