"""Tasks: scheduled/automated jobs — distinct from Notes' todos (see JARVIS
Plan's Phase 3 scoping: "not to be confused with Notes' todos"). Formalizes
what bridge_sync.py's poll cycle + proactive nudges already do today into a
real scheduler.

Two schedule kinds for this pass:
- "once": fires a single time at run_at (ISO datetime string), then disables itself.
- "interval": fires every interval_seconds, computing the next run each time.

Chained tasks, webhook triggers, and per-task personas (all real Odysseus
features) are deliberately out of this pass — this is the execution-loop
foundation those build on top of.
"""
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
import os

TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
TASK_RUNS_FILE = os.path.join(DATA_DIR, "task_runs.json")

MAX_RUNS_KEPT = 200


class TaskService:
    def __init__(self) -> None:
        self._tasks: dict = read_json(TASKS_FILE, {})
        self._runs: list = read_json(TASK_RUNS_FILE, [])

    def _save_tasks(self) -> None:
        write_json_atomic(TASKS_FILE, self._tasks)

    def _save_runs(self) -> None:
        write_json_atomic(TASK_RUNS_FILE, self._runs[-MAX_RUNS_KEPT:])

    def list_tasks(self) -> list[dict]:
        return sorted(self._tasks.values(), key=lambda t: t["created_at"], reverse=True)

    def get_task(self, task_id: str) -> Optional[dict]:
        return self._tasks.get(task_id)

    def create_task(
        self,
        name: str,
        prompt: str,
        schedule_kind: str,
        run_at: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        builtin_action: Optional[str] = None,
        deliver_to_channel: Optional[str] = None,
    ) -> dict:
        if schedule_kind not in ("once", "interval"):
            raise ValueError("schedule_kind must be 'once' or 'interval'")
        if schedule_kind == "once" and not run_at:
            raise ValueError("run_at is required for a one-shot task")
        if schedule_kind == "interval" and not interval_seconds:
            raise ValueError("interval_seconds is required for a recurring task")

        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        next_run_at = run_at if schedule_kind == "once" else _iso_in(interval_seconds)
        task = {
            "id": task_id,
            "name": name,
            "prompt": prompt,
            "schedule_kind": schedule_kind,
            "run_at": run_at,
            "interval_seconds": interval_seconds,
            "enabled": True,
            "next_run_at": next_run_at,
            "last_run_at": None,
            "created_at": now,
            # Set when this task is a premade "built-in" (David's ask
            # 2026-08-31, see core/builtin_tasks.py) — the scheduler runs
            # the registered action/prompt-builder instead of the static
            # `prompt` field above, which is just a human-readable label here.
            "builtin_action": builtin_action,
            # Settings > Channels delivery target (David's ask 2026-08-31) —
            # None means "Tasks tab only," matching existing behavior exactly.
            "deliver_to_channel": deliver_to_channel,
        }
        self._tasks[task_id] = task
        self._save_tasks()
        return task

    def update_task(self, task_id: str, **fields) -> dict:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        for key in ("name", "prompt", "enabled", "deliver_to_channel"):
            if key in fields:
                task[key] = fields[key]
        self._save_tasks()
        return task

    def delete_task(self, task_id: str) -> None:
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._save_tasks()

    def due_tasks(self) -> list[dict]:
        now_iso = datetime.now(timezone.utc).isoformat()
        return [
            t for t in self._tasks.values()
            if t["enabled"] and t["next_run_at"] and t["next_run_at"] <= now_iso
        ]

    def record_run(self, task_id: str, output: str, error: Optional[str] = None, delivered: Optional[bool] = None) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        now = time.time()
        self._runs.append({
            "task_id": task_id,
            "task_name": task["name"],
            "ran_at": now,
            "output": output,
            "error": error,
            # None = no delivery channel configured for this task; True/False =
            # a channel was configured and the send did/didn't succeed (David's
            # ask 2026-09-02 — a failed Discord delivery was previously silent
            # everywhere but the server log).
            "delivered": delivered,
        })
        self._save_runs()

        task["last_run_at"] = now
        if task["schedule_kind"] == "once":
            task["enabled"] = False
            task["next_run_at"] = None
        else:
            task["next_run_at"] = _iso_in(task["interval_seconds"])
        self._save_tasks()

    def list_runs(self, task_id: Optional[str] = None) -> list[dict]:
        runs = self._runs if task_id is None else [r for r in self._runs if r["task_id"] == task_id]
        return sorted(runs, key=lambda r: r["ran_at"], reverse=True)


def _iso_in(seconds: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


task_service = TaskService()
