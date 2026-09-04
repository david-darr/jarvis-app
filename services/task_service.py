"""Tasks: scheduled/automated jobs — distinct from Notes' todos (see JARVIS
Plan's Phase 3 scoping: "not to be confused with Notes' todos"). Formalizes
what bridge_sync.py's poll cycle + proactive nudges already do today into a
real scheduler.

Three schedule kinds:
- "once": fires a single time at run_at (ISO datetime string), then disables itself.
- "interval": fires every interval_seconds, computing the next run each time.
- "daily": fires once a day at run_time ("HH:MM", the machine's LOCAL time).

"daily" exists because "interval" cannot express it (David's ask 2026-09-04,
"once everyday at 6:00 am"). An interval task computes its next run as
now + interval_seconds at each run, so a 24h interval is anchored to whenever
you happened to create it and drifts a little further every cycle — there is
no way to pin it to a wall-clock time. Local time, not UTC, because "6:00 am"
means six in the morning where the user is.

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
        run_time: Optional[str] = None,
    ) -> dict:
        if schedule_kind not in ("once", "interval", "daily"):
            raise ValueError("schedule_kind must be 'once', 'interval' or 'daily'")
        if schedule_kind == "once" and not run_at:
            raise ValueError("run_at is required for a one-shot task")
        if schedule_kind == "interval" and not interval_seconds:
            raise ValueError("interval_seconds is required for a recurring task")
        if schedule_kind == "daily":
            if not run_time:
                raise ValueError("run_time (HH:MM) is required for a daily task")
            _parse_hhmm(run_time)  # raises ValueError on anything malformed

        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        if schedule_kind == "once":
            next_run_at = run_at
        elif schedule_kind == "daily":
            next_run_at = _next_daily(run_time)
        else:
            next_run_at = _iso_in(interval_seconds)
        task = {
            "id": task_id,
            "name": name,
            "prompt": prompt,
            "schedule_kind": schedule_kind,
            "run_at": run_at,
            "interval_seconds": interval_seconds,
            # "HH:MM" in local time, for schedule_kind == "daily".
            "run_time": run_time,
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

    def set_daily_schedule(self, task_id: str, run_time: str) -> dict:
        """Convert an existing task to a daily wall-clock schedule.

        Separate from update_task() on purpose: that one only takes the fields
        the Tasks UI can edit, and changing a schedule has to recompute
        next_run_at or the task would keep firing on its old cadence.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        _parse_hhmm(run_time)
        task["schedule_kind"] = "daily"
        task["run_time"] = run_time
        task["interval_seconds"] = None
        task["next_run_at"] = _next_daily(run_time)
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
        elif task["schedule_kind"] == "daily":
            task["next_run_at"] = _next_daily(task["run_time"])
        else:
            task["next_run_at"] = _iso_in(task["interval_seconds"])
        self._save_tasks()

    def list_runs(self, task_id: Optional[str] = None) -> list[dict]:
        runs = self._runs if task_id is None else [r for r in self._runs if r["task_id"] == task_id]
        return sorted(runs, key=lambda r: r["ran_at"], reverse=True)


def _iso_in(seconds: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _parse_hhmm(run_time: str) -> tuple[int, int]:
    """"HH:MM" -> (hour, minute), rejecting anything that isn't a real time."""
    try:
        hh, mm = str(run_time).strip().split(":")
        hour, minute = int(hh), int(mm)
    except (ValueError, AttributeError):
        raise ValueError(f"run_time must look like 'HH:MM', got {run_time!r}")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"run_time out of range: {run_time!r}")
    return hour, minute


def _next_daily(run_time: str) -> str:
    """Next occurrence of a local wall-clock time, as a UTC ISO string.

    Stored in UTC because due_tasks() compares against a UTC "now" — but
    computed from local time, since "6:00 am" means six in the morning here,
    not in UTC. Uses astimezone() with no argument, which picks up the
    machine's real local zone (and its current DST offset).
    """
    hour, minute = _parse_hhmm(run_time)
    now_local = datetime.now().astimezone()
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_local:
        from datetime import timedelta
        target += timedelta(days=1)
    return target.astimezone(timezone.utc).isoformat()


task_service = TaskService()
