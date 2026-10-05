"""Webhook triggers (services/trigger_service.py). Managing triggers is
admin-only: a trigger lets an outside service start work. The event endpoint
is public by necessity and trusts nothing until the signature is verified."""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin
from services.trigger_service import trigger_service

router = APIRouter(prefix="/api/triggers", tags=["triggers"])
logger = logging.getLogger(__name__)


class Condition(BaseModel):
    path: str
    equals: str = ""


class TriggerBody(BaseModel):
    name: Optional[str] = None
    preset: Optional[str] = None
    action: Optional[str] = None
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    endpoint_id: Optional[str] = None
    events: Optional[list[str]] = None
    conditions: Optional[list[Condition]] = None
    title_template: Optional[str] = None
    prompt_template: Optional[str] = None
    auto_run: Optional[bool] = None
    deliver_to_channel: Optional[str] = None
    enabled: Optional[bool] = None


class AnswerBody(BaseModel):
    approve: bool


def start_run(task: dict) -> None:
    """A task or goal run with the event attached, in the background, the
    same way the scheduler runs it."""
    from core.task_scheduler import _run_task
    asyncio.get_running_loop().create_task(_run_task(task))


def _fields(body: TriggerBody) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if "conditions" in fields:
        fields["conditions"] = [c if isinstance(c, dict) else c.model_dump() for c in fields["conditions"] or []]
    return fields


@router.get("")
async def list_triggers(user: str = Depends(require_admin)) -> dict:
    return {"triggers": [trigger_service.public(t) for t in trigger_service.triggers()],
            "pending": trigger_service.pending()}


@router.post("")
async def create_trigger(body: TriggerBody, user: str = Depends(require_admin)) -> dict:
    fields = _fields(body)
    try:
        trigger, secret = trigger_service.create(fields.pop("name", ""), **fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # The secret is shown this once; afterwards only a new one can be made.
    return {**trigger_service.public(trigger), "secret": secret}


@router.patch("/{trigger_id}")
async def update_trigger(trigger_id: str, body: TriggerBody, user: str = Depends(require_admin)) -> dict:
    try:
        return trigger_service.public(trigger_service.update(trigger_id, **_fields(body)))
    except KeyError:
        raise HTTPException(status_code=404, detail="trigger not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{trigger_id}/secret")
async def new_secret(trigger_id: str, user: str = Depends(require_admin)) -> dict:
    try:
        return {"secret": trigger_service.new_secret(trigger_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="trigger not found")


@router.delete("/{trigger_id}")
async def delete_trigger(trigger_id: str, user: str = Depends(require_admin)) -> dict:
    try:
        trigger_service.delete(trigger_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="trigger not found")
    return {"ok": True}


@router.post("/pending/{pending_id}")
async def answer_pending(pending_id: str, body: AnswerBody, user: str = Depends(require_admin)) -> dict:
    try:
        outcome, run = trigger_service.answer(pending_id, body.approve)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'"))
    if run:
        start_run(run)
    return {"ok": True, "next": outcome}


@router.post("/{trigger_id}", include_in_schema=False)
async def receive(trigger_id: str, request: Request) -> JSONResponse:
    """Public: called by the outside service. Nothing is read or acted on
    before the signature checks out (trigger_service.receive)."""
    if int(request.headers.get("content-length") or 0) > 2 * 1_000_000:
        return JSONResponse({"error": "too large"}, status_code=413)
    body = await request.body()
    status, content, work = trigger_service.receive(trigger_id, dict(request.headers), body)
    if work:
        try:
            started = trigger_service.start(work)
        except Exception as e:  # a removed agent or model: logged here and on the trigger's page
            logger.exception("trigger %s: could not start its work", trigger_id)
            trigger_service.record_failure(work, str(e))
            return JSONResponse({"error": "could not start; see JARVIS"}, status_code=500)
        if started.get("run"):
            start_run(started["run"])
        if started.get("pending"):
            trigger_service.notify(work["trigger"], started["pending"])
            content = {**content, "waiting_for_approval": True}
    return JSONResponse(content, status_code=status)
