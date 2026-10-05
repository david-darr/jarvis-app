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

Cards (the work board, Hermes track 2026-09-23, after Hermes's kanban): a
fourth kind, "card", with no schedule. A card is a one-off piece of work that
moves Backlog -> Ready -> Running -> Review -> Done (or Blocked). The task
loop's dispatcher (core/task_scheduler.py) claims one Ready card at a time on
a lease, runs it, and puts the result in Review for a person to approve or
send back with a note. A card can wait on other cards (depends_on): it is not
claimed until they are all Done, and their results are handed to it - which
is how chained work happens. Any task or card can name the model it runs on
(endpoint_id); without one it runs on Claude, as tasks always have.

Agents (services/agent_service.py, 2026-10-04): a task or card with
`agent_id` is that agent's work - a card is a job it was given, a scheduled
task is one of its standing goals. It runs on the agent's model with the
agent's identity and memory; this module only stores and schedules it.
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

# Kept per task, so a task that runs every few minutes cannot push another
# task's or card's history out (one shared cap of 200 did, 2026-10-02).
MAX_RUNS_PER_TASK = 50

CARD_STATUSES = ("backlog", "ready", "running", "review", "done", "blocked")
# One run plus two retries, then Blocked.
CARD_MAX_ATTEMPTS = 3
# How long a claim holds. A run still going past it is taken as dead (the app
# closed mid-run, say) and the card goes back to Ready, or Blocked when out of
# attempts, so it can never sit in Running forever.
CARD_LEASE_SECONDS = 30 * 60
CARD_RESULT_INSTRUCTION = (
    "[This is a card on JARVIS's work board. Your reply is the result the user will review, so reply with the "
    "finished work itself, not a note about where you put it. Use tools only when the work itself needs them.]"
)


class TaskService:
    def __init__(self) -> None:
        self._tasks: dict = read_json(TASKS_FILE, {})
        self._runs: list = read_json(TASK_RUNS_FILE, [])

    def _save_tasks(self) -> None:
        write_json_atomic(TASKS_FILE, self._tasks)

    def _save_runs(self) -> None:
        counts: dict = {}
        kept = []
        for run in reversed(self._runs):
            seen = counts.get(run["task_id"], 0)
            if seen < MAX_RUNS_PER_TASK:
                kept.append(run)
                counts[run["task_id"]] = seen + 1
        self._runs = kept[::-1]
        write_json_atomic(TASK_RUNS_FILE, self._runs)

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
        depends_on: Optional[list[str]] = None,
        status: Optional[str] = None,
        endpoint_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        report_when: Optional[str] = None,
        trigger: Optional[dict] = None,
    ) -> dict:
        if schedule_kind not in ("once", "interval", "daily", "card"):
            raise ValueError("schedule_kind must be 'once', 'interval', 'daily' or 'card'")
        if schedule_kind != "card" and (depends_on or status):
            raise ValueError("depends_on and status are for cards (schedule_kind 'card')")
        if schedule_kind == "card":
            if status not in (None, "backlog", "ready"):
                raise ValueError("a new card starts in 'backlog' or 'ready'")
            self._check_dependencies(None, depends_on or [])
        if schedule_kind == "once" and not run_at:
            raise ValueError("run_at is required for a one-shot task")
        if schedule_kind == "interval" and not interval_seconds:
            raise ValueError("interval_seconds is required for a recurring task")
        if schedule_kind == "daily":
            if not run_time:
                raise ValueError("run_time (HH:MM) is required for a daily task")
            _parse_hhmm(run_time)  # raises ValueError on anything malformed
        if agent_id:
            from services.agent_service import agent_service
            if agent_service.get(agent_id) is None:
                raise ValueError(f"agent_id must name an existing agent; {agent_id!r} is not one")
        if report_when not in (None, "always", "notable"):
            raise ValueError("report_when is 'always' or 'notable'")

        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        if schedule_kind == "card":
            next_run_at = None  # cards are dispatched, not scheduled
        elif schedule_kind == "once":
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
            # The model endpoint this runs on (core/model_endpoints.py); None
            # keeps the original behaviour, Claude through the CLI.
            "endpoint_id": endpoint_id,
            # The agent this is work for (services/agent_service.py); None for
            # ordinary tasks. A goal reports only when notable unless 'always'.
            "agent_id": agent_id,
            "report_when": report_when or ("notable" if agent_id and schedule_kind != "card" else None),
            # The webhook trigger that made this card ({id, name};
            # services/trigger_service.py), so the board can say where it came from.
            "trigger": trigger,
        }
        if schedule_kind == "card":
            task.update({"status": status or "backlog", "depends_on": list(depends_on or []), "attempts": 0,
                         "claimed_until": None, "comments": []})
        self._tasks[task_id] = task
        self._save_tasks()
        return task

    def update_task(self, task_id: str, **fields) -> dict:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        if "depends_on" in fields:
            if task["schedule_kind"] != "card":
                raise ValueError("depends_on is for cards")
            self._check_dependencies(task_id, fields["depends_on"] or [])
        if fields.get("report_when") not in (None, "always", "notable"):
            raise ValueError("report_when is 'always' or 'notable'")
        for key in ("name", "prompt", "enabled", "deliver_to_channel", "endpoint_id", "depends_on", "report_when"):
            if key in fields:
                task[key] = list(fields[key] or []) if key == "depends_on" else fields[key]
        self._save_tasks()
        return task

    # -- cards ----------------------------------------------------------------

    def _check_dependencies(self, card_id: Optional[str], depends_on: list[str]) -> None:
        for dep in depends_on:
            other = self._tasks.get(dep)
            if other is None or other["schedule_kind"] != "card":
                raise ValueError(f"depends_on must name existing cards; {dep!r} is not one")
            if dep == card_id:
                raise ValueError("a card cannot depend on itself")
        if card_id is None:
            return
        # Would this make a loop? Walk what the new dependencies wait on.
        seen, stack = set(), list(depends_on)
        while stack:
            current = stack.pop()
            if current == card_id:
                raise ValueError("that would make cards wait on each other in a loop")
            if current not in seen:
                seen.add(current)
                stack.extend(self._tasks.get(current, {}).get("depends_on") or [])

    def _comment(self, card: dict, kind: str, text: str, by: str) -> None:
        card["comments"].append({"at": time.time(), "kind": kind, "text": text, "by": by})

    def waiting_on(self, card: dict) -> list[str]:
        """Dependencies not yet Done."""
        return [d for d in card.get("depends_on") or [] if (self._tasks.get(d) or {}).get("status") != "done"]

    def set_card_status(self, card_id: str, status: str, note: Optional[str] = None, by: str = "user") -> dict:
        """A person (or an agent) moving a card. Running is the dispatcher's
        alone, so a running card cannot be moved and nothing can be moved to
        it. Review -> Ready with a note is "request changes": the note goes
        to the next run. Blocked -> Ready is a retry and restores the attempts."""
        card = self._tasks.get(card_id)
        if card is None or card["schedule_kind"] != "card":
            raise KeyError(f"no such card: {card_id}")
        if status not in CARD_STATUSES or status == "running":
            raise ValueError(f"a card can be moved to backlog, ready, review, done or blocked, not {status!r}")
        if card["status"] == "running":
            raise ValueError("this card is running; wait for it to finish")
        if note:
            kind = "feedback" if card["status"] == "review" and status == "ready" else "note"
            self._comment(card, kind, note, by)
            if kind == "feedback" and card.get("agent_id"):
                # A correction is something the agent should carry from now on,
                # not only on this card's re-run.
                from services.agent_service import agent_service
                if agent_service.get(card["agent_id"]):
                    try:
                        agent_service.remember(card["agent_id"], "Corrections", f"On \"{card['name']}\": {note}")
                    except ValueError:
                        pass  # memory full: the note still reaches this card's re-run
        if status == "ready" and card["status"] == "blocked":
            card["attempts"] = 0
        card["status"] = status
        self._save_tasks()
        return card

    def claim_next_card(self, now: Optional[float] = None, card_id: Optional[str] = None,
                        eligible=None) -> Optional[dict]:
        """The oldest Ready card whose dependencies are all Done, marked
        Running with a lease. One at a time keeps spend predictable.
        card_id claims that card only ("Run now"), from Backlog too.
        eligible(card) can hold a card back (an agent turned off or out of
        runs for today) without moving it."""
        now = time.time() if now is None else now
        allowed = ("ready", "backlog") if card_id else ("ready",)
        ready = [c for c in self._tasks.values()
                 if c["schedule_kind"] == "card" and c["status"] in allowed and not self.waiting_on(c)
                 and (card_id is None or c["id"] == card_id) and (eligible is None or eligible(c))]
        if not ready:
            return None
        card = min(ready, key=lambda c: c["created_at"])
        card["status"] = "running"
        card["attempts"] += 1
        card["claimed_until"] = now + CARD_LEASE_SECONDS
        card["run_started_at"] = now
        self._save_tasks()
        return card

    def finish_card(self, card_id: str, output: str) -> dict:
        card = self._tasks[card_id]
        self._comment(card, "result", output, "jarvis")
        card["status"] = "review"
        card["claimed_until"] = None
        self._append_run(card, output, None, "succeeded")
        self._save_tasks()
        return card

    def fail_card(self, card_id: str, error: str, lost: bool = False) -> dict:
        card = self._tasks[card_id]
        self._comment(card, "error", error, "jarvis")
        card["status"] = "blocked" if card["attempts"] >= CARD_MAX_ATTEMPTS else "ready"
        card["claimed_until"] = None
        self._append_run(card, "", error, "lost" if lost else "failed")
        self._save_tasks()
        return card

    def reclaim_stale_cards(self, now: Optional[float] = None) -> list[dict]:
        """Running cards whose lease ran out: the run is taken as lost."""
        now = time.time() if now is None else now
        stale = [c for c in self._tasks.values() if c["schedule_kind"] == "card" and c["status"] == "running"
                 and (c.get("claimed_until") or 0) < now]
        return [self.fail_card(c["id"], "The run did not finish (JARVIS may have closed while it ran).", lost=True)
                for c in stale]

    def card_prompt(self, card: dict) -> str:
        """What the model is asked: the card itself, the results of the
        cards it waited on, and, on a re-run, its last result with the
        feedback on it."""
        parts = [card["prompt"]]
        for dep in card.get("depends_on") or []:
            result = self._latest(self._tasks.get(dep) or {}, "result")
            if result:
                parts.append(f"[Result of the card this one depends on, \"{self._tasks[dep]['name']}\":]\n{result['text']}")
        feedback = self._latest(card, "feedback")
        previous = self._latest(card, "result")
        if feedback and previous and previous["at"] < feedback["at"]:
            parts.append(f"[Your previous result:]\n{previous['text']}\n\n[Feedback on it, to address now:]\n{feedback['text']}")
        # Without this, a model with tools filed the work elsewhere (a note)
        # and returned "the note has been created" as the card's result
        # (seen live on qwen2.5-coder, 2026-09-23).
        parts.append(CARD_RESULT_INSTRUCTION)
        return "\n\n".join(parts)

    @staticmethod
    def _latest(card: dict, kind: str) -> Optional[dict]:
        return next((c for c in reversed(card.get("comments") or []) if c["kind"] == kind), None)

    def _append_run(self, task: dict, output: str, error: Optional[str], outcome: str,
                    delivered: Optional[bool] = None) -> None:
        """One run record. outcome is "succeeded", "failed" or "lost" (a run
        that never reported back: the app closed mid-run, or a card's lease
        ran out). The start time is the one mark_started() or a card's claim
        left on the task; the caller saves the task."""
        now = time.time()
        started = task.pop("run_started_at", None)
        self._runs.append({
            "task_id": task["id"],
            "task_name": task["name"],
            "started_at": started,
            "ran_at": now,  # when it finished; the name predates started_at
            "duration_seconds": round(now - started, 1) if started else None,
            "outcome": outcome,
            "output": output,
            "error": error,
            # None = no delivery channel configured for this task; True/False =
            # a channel was configured and the send did/didn't succeed (David's
            # ask 2026-09-02 — a failed Discord delivery was previously silent
            # everywhere but the server log).
            "delivered": delivered,
            # The model as named when it ran, so the history still reads
            # right after the task moves to another model or it is removed.
            "endpoint_id": task.get("endpoint_id"),
            "model": _model_label(task.get("endpoint_id")),
            "attempt": task.get("attempts") if task["schedule_kind"] == "card" else None,
        })
        task["last_run_at"] = now
        self._save_runs()

    def mark_started(self, task_id: str) -> None:
        """A scheduled task's run begins. Persisted, so a run cut off by the
        app closing is still found and recorded on the next start."""
        task = self._tasks.get(task_id)
        if task is not None:
            task["run_started_at"] = time.time()
            self._save_tasks()

    def recover_interrupted_runs(self) -> list[dict]:
        """At startup nothing is running yet, so a scheduled task still marked
        as started was cut off: record it as lost. Cards are left to their
        lease (reclaim_stale_cards), which already covers this."""
        lost = [t for t in self._tasks.values() if t["schedule_kind"] != "card" and t.get("run_started_at")]
        for task in lost:
            self._append_run(task, "", "The run did not finish (JARVIS closed while it ran).", "lost")
        if lost:
            self._save_tasks()
        return lost

    def skip_occurrence(self, task_id: str) -> None:
        """Move a due goal to its next time without running it (its agent is
        off or out of runs today). Without this the loop would find it due
        again every 15 seconds."""
        task = self._tasks.get(task_id)
        if task is None or task["schedule_kind"] == "card":
            return
        if task["schedule_kind"] == "once":
            task["enabled"] = False
            task["next_run_at"] = None
        elif task["schedule_kind"] == "daily":
            task["next_run_at"] = _next_daily(task["run_time"])
        else:
            task["next_run_at"] = _iso_in(task["interval_seconds"])
        self._save_tasks()

    def delete_agent_work(self, agent_id: str) -> int:
        """An agent deleted: its goals and unfinished cards go; finished run
        history stays, under the name it had."""
        doomed = [t["id"] for t in self._tasks.values() if t.get("agent_id") == agent_id
                  and not (t["schedule_kind"] == "card" and t.get("status") in ("done", "running"))]
        for task_id in doomed:
            del self._tasks[task_id]
            for other in self._tasks.values():
                if task_id in (other.get("depends_on") or []):
                    other["depends_on"] = [d for d in other["depends_on"] if d != task_id]
        if doomed:
            self._save_tasks()
        return len(doomed)

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
            # The Tasks tab says a deleted task's history goes with it.
            if any(r["task_id"] == task_id for r in self._runs):
                self._runs = [r for r in self._runs if r["task_id"] != task_id]
                self._save_runs()
            # A card that waited on this one no longer does.
            for other in self._tasks.values():
                if task_id in (other.get("depends_on") or []):
                    other["depends_on"] = [d for d in other["depends_on"] if d != task_id]
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
        self._append_run(task, output, error, "failed" if error else "succeeded", delivered)
        if task["schedule_kind"] == "once":
            task["enabled"] = False
            task["next_run_at"] = None
        elif task["schedule_kind"] == "daily":
            task["next_run_at"] = _next_daily(task["run_time"])
        else:
            task["next_run_at"] = _iso_in(task["interval_seconds"])
        self._save_tasks()

    def list_runs(self, task_id: Optional[str] = None) -> list[dict]:
        """Newest first. Records from before 2026-10-02 have no start time or
        outcome: the outcome is read from the error and the duration stays
        unknown rather than guessed."""
        runs = self._runs if task_id is None else [r for r in self._runs if r["task_id"] == task_id]
        return [{"started_at": None, "duration_seconds": None, "model": None, "attempt": None, **r,
                 "outcome": r.get("outcome") or ("failed" if r.get("error") else "succeeded")}
                for r in sorted(runs, key=lambda r: r["ran_at"], reverse=True)]


def _model_label(endpoint_id: Optional[str]) -> str:
    if not endpoint_id:
        return "Claude"
    from core import model_endpoints
    endpoint = model_endpoints.get_endpoint(endpoint_id)
    return endpoint["name"] if endpoint else "a removed model"


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
