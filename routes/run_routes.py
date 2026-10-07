"""Run timelines (roadmap phase 8, 2026-10-06; spec: the vault note
"Operations - Phase 8 (Build Spec)"): every model run JARVIS made - a chat
turn, a task, card, goal, helper or agent run - and what it did, step by
step (core/runs.py, the session store's `runs` and `run_events`).

Admin only: a run's steps quote what tools were given and returned.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core import helpers, session_manager_store as store
from core.middleware import require_admin

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _label(run: dict) -> str:
    """What a person would call the work: the chat's title, the task's name."""
    if run.get("session_id"):
        session = store.get_session_header(run["session_id"])
        if session:
            return session[0].get("title") or "Untitled chat"
    if run.get("task_id"):
        from services.task_service import task_service
        task = task_service.get_task(run["task_id"])
        if task:
            return task["name"]
    if run.get("agent_id"):
        from services.agent_service import agent_service
        agent = agent_service.get(run["agent_id"])
        if agent:
            return agent["name"]
    return ""


@router.get("")
async def list_runs(session_id: Optional[str] = None, task_id: Optional[str] = None, agent_id: Optional[str] = None,
                    outcome: Optional[str] = None, limit: int = 100, user: str = Depends(require_admin)) -> list[dict]:
    if outcome and outcome not in ("finished", "failed", "stopped"):
        raise HTTPException(status_code=400, detail="outcome is finished, failed or stopped")
    return [{**run, "label": _label(run)}
            for run in store.find_runs(session_id=session_id, task_id=task_id, agent_id=agent_id, outcome=outcome,
                                       limit=limit)]


@router.get("/{run_id}")
async def get_run(run_id: str, user: str = Depends(require_admin)) -> dict:
    """One run: its row, its steps, and what hangs off it - the run that
    started it, the runs it started, its helpers, and for a task its
    delivery."""
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    detail = {**run, "label": _label(run), "steps": store.run_events(run_id),
              "parent": None, "children": store.find_runs(parent_id=run_id, limit=50),
              "helpers": [{k: h[k] for k in ("id", "goal", "status", "tokens", "error")}
                          for h in helpers.batch_rows_for_run(run_id)],
              "task_run": None}
    if run.get("parent_id"):
        parent = store.get_run(run["parent_id"])
        detail["parent"] = parent and {**parent, "label": _label(parent)}
    if run.get("task_id"):
        from services.task_service import task_service
        record = next((r for r in task_service.list_runs(run["task_id"]) if r.get("run_id") == run_id), None)
        if record:
            detail["task_run"] = {k: record.get(k) for k in ("outcome", "delivered", "delivery", "late_seconds", "source")}
    return detail
