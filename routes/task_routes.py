"""Task CRUD + manual "run now" + run history — the Tasks tab."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core import model_endpoints
from core.builtin_tasks import BUILTIN_TASKS, list_builtin_tasks
from core.middleware import require_user
from core.task_scheduler import _run_task
from services.task_service import task_service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    name: str
    prompt: str
    schedule_kind: str  # "once" | "interval" | "daily" | "card"
    run_at: Optional[str] = None
    interval_seconds: Optional[int] = None
    deliver_to_channel: Optional[str] = None
    run_time: Optional[str] = None  # "HH:MM" local, for schedule_kind="daily"
    # Cards (the work board): see services/task_service.py.
    depends_on: Optional[list[str]] = None
    status: Optional[str] = None  # a new card: "backlog" (default) or "ready"
    endpoint_id: Optional[str] = None  # any task: the model it runs on


class UpdateTaskRequest(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    enabled: Optional[bool] = None
    deliver_to_channel: Optional[str] = None
    depends_on: Optional[list[str]] = None
    endpoint_id: Optional[str] = None


class CardStatusRequest(BaseModel):
    status: str
    note: Optional[str] = None


def _check_endpoint(endpoint_id: Optional[str]) -> None:
    if endpoint_id and model_endpoints.get_endpoint(endpoint_id) is None:
        raise HTTPException(status_code=400, detail="endpoint_id must name a model added in Settings")


@router.get("")
async def list_tasks(user: str = Depends(require_user)) -> list[dict]:
    return task_service.list_tasks()


# Built-in premade tasks (David's ask 2026-08-31, matching Odysseus's
# builtin action registry) — registered BEFORE /{task_id} so "builtin"
# isn't swallowed by the dynamic task_id route.
@router.get("/builtin")
async def get_builtin_tasks(user: str = Depends(require_user)) -> list[dict]:
    enabled_by_action = {t["builtin_action"]: t["id"] for t in task_service.list_tasks() if t.get("builtin_action")}
    return [
        {**b, "enabled": b["action_id"] in enabled_by_action, "task_id": enabled_by_action.get(b["action_id"])}
        for b in list_builtin_tasks()
    ]


class EnableBuiltinRequest(BaseModel):
    deliver_to_channel: Optional[str] = None


@router.post("/builtin/{action_id}/enable")
async def enable_builtin_task(action_id: str, body: Optional[EnableBuiltinRequest] = None, user: str = Depends(require_user)) -> dict:
    if action_id not in BUILTIN_TASKS:
        raise HTTPException(status_code=404, detail="unknown built-in task")
    existing = next((t for t in task_service.list_tasks() if t.get("builtin_action") == action_id), None)
    if existing:
        return existing
    defn = BUILTIN_TASKS[action_id]
    # A built-in with a default_daily_time wants a wall-clock slot, not a
    # drifting 24h interval — the Daily Brief is only a "daily brief" if it
    # lands in the morning (David, 2026-09-04).
    daily_time = defn.get("default_daily_time")
    if daily_time:
        return task_service.create_task(
            name=defn["label"],
            prompt=f"(built-in: {defn['label']})",
            schedule_kind="daily",
            run_time=daily_time,
            builtin_action=action_id,
            deliver_to_channel=(body.deliver_to_channel if body else None),
        )
    return task_service.create_task(
        name=defn["label"],
        prompt=f"(built-in: {defn['label']})",
        schedule_kind="interval",
        interval_seconds=defn["default_interval_seconds"],
        builtin_action=action_id,
        deliver_to_channel=(body.deliver_to_channel if body else None),
    )


@router.get("/{task_id}")
async def get_task(task_id: str, user: str = Depends(require_user)) -> dict:
    task = task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.post("")
async def create_task(body: CreateTaskRequest, user: str = Depends(require_user)) -> dict:
    _check_endpoint(body.endpoint_id)
    try:
        return task_service.create_task(
            body.name, body.prompt, body.schedule_kind, body.run_at, body.interval_seconds,
            deliver_to_channel=body.deliver_to_channel, run_time=body.run_time,
            depends_on=body.depends_on, status=body.status, endpoint_id=body.endpoint_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{task_id}")
async def update_task(task_id: str, body: UpdateTaskRequest, user: str = Depends(require_user)) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    _check_endpoint(fields.get("endpoint_id"))
    try:
        return task_service.update_task(task_id, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail="task not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/status")
async def set_card_status(task_id: str, body: CardStatusRequest, user: str = Depends(require_user)) -> dict:
    """Move a card on the work board. Review -> Ready with a note is
    "request changes"; Review -> Done is approval; Blocked -> Ready retries."""
    try:
        return task_service.set_card_status(task_id, body.status, note=body.note, by=user)
    except KeyError:
        raise HTTPException(status_code=404, detail="card not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{task_id}")
async def delete_task(task_id: str, user: str = Depends(require_user)) -> dict:
    task_service.delete_task(task_id)
    return {"ok": True}


@router.post("/{task_id}/run")
async def run_task_now(task_id: str, user: str = Depends(require_user)) -> dict:
    task = task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.get("agent_id"):
        from services.agent_service import agent_service
        may_run, why_not = agent_service.can_run(task["agent_id"])
        if not may_run:
            raise HTTPException(status_code=409, detail=f"Not run: {why_not}.")
    if task["schedule_kind"] == "card":
        # A card runs through the board's claim, so it cannot run twice at once
        # or ahead of the cards it waits on.
        task = task_service.claim_next_card(card_id=task_id)
        if task is None:
            raise HTTPException(status_code=409, detail="this card is running, finished, or still waiting on other cards")
    await _run_task(task)
    runs = task_service.list_runs(task_id)
    return runs[0] if runs else {"ok": True}


@router.get("/{task_id}/runs")
async def list_task_runs(task_id: str, user: str = Depends(require_user)) -> list[dict]:
    return task_service.list_runs(task_id)
