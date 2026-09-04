"""Task CRUD + manual "run now" + run history — the Tasks tab."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.builtin_tasks import BUILTIN_TASKS, list_builtin_tasks
from core.middleware import require_user
from core.task_scheduler import _run_task
from services.task_service import task_service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    name: str
    prompt: str
    schedule_kind: str  # "once" | "interval" | "daily"
    run_at: Optional[str] = None
    interval_seconds: Optional[int] = None
    deliver_to_channel: Optional[str] = None
    run_time: Optional[str] = None  # "HH:MM" local, for schedule_kind="daily"


class UpdateTaskRequest(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    enabled: Optional[bool] = None
    deliver_to_channel: Optional[str] = None


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
    try:
        return task_service.create_task(
            body.name, body.prompt, body.schedule_kind, body.run_at, body.interval_seconds,
            deliver_to_channel=body.deliver_to_channel, run_time=body.run_time,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{task_id}")
async def update_task(task_id: str, body: UpdateTaskRequest, user: str = Depends(require_user)) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return task_service.update_task(task_id, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail="task not found")


@router.delete("/{task_id}")
async def delete_task(task_id: str, user: str = Depends(require_user)) -> dict:
    task_service.delete_task(task_id)
    return {"ok": True}


@router.post("/{task_id}/run")
async def run_task_now(task_id: str, user: str = Depends(require_user)) -> dict:
    task = task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    await _run_task(task)
    runs = task_service.list_runs(task_id)
    return runs[0] if runs else {"ok": True}


@router.get("/{task_id}/runs")
async def list_task_runs(task_id: str, user: str = Depends(require_user)) -> list[dict]:
    return task_service.list_runs(task_id)
