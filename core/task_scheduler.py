"""Background loop that executes due scheduled tasks. Started once at app
startup (see app.py's lifespan), stopped at shutdown.

Each due task gets its own short-lived Brain (connect, run the task's prompt
once, disconnect) rather than reusing a long-lived connection — task runs are
independent one-shot executions, not a persisted conversation the way chat
sessions are.

Durable work (roadmap phase 4, 2026-10-06; spec: the vault note "Durable
Work - Phase 4 (Build Spec)"):
- Every run, whoever starts it (the schedule, Run now, a trigger, an answered
  question), goes through start_run(), which holds one lock per task: the
  same task never runs twice at once. Before this, two clicks of Run now were
  two paid runs.
- Two lanes: due scheduled tasks and the next work-board card each run in
  the background, one at a time per lane, so a long card no longer holds up
  a 6:00 brief while the loop waits on it.
- stop_run() cancels a running task, card or goal.
- A run's channel message goes through the outbox (core/outbox.py), which
  this loop also drives.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

from core import events, file_checkpoints, logs as log_files, outbox, runs, token_usage
from core.brain import Brain
from core.builtin_tasks import BUILTIN_TASKS
from services.agent_service import agent_service, is_silent
from services.task_service import task_service

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15

_loop_task: asyncio.Task | None = None
# One lock per task id: the run in progress holds it (start_run). Kept
# with its event loop, since an asyncio lock belongs to the loop it is used on.
_locks: dict[str, tuple] = {}
# The work of each run in progress, by task id, for stop_run().
_running: dict[str, asyncio.Task] = {}
# The two lanes' current background run (_tick).
_lanes: dict[str, Optional[asyncio.Task]] = {"schedule": None, "card": None}


def _deliver(task: dict, output: str) -> Optional[str]:
    """Settings > Channels delivery (David's ask 2026-08-31: task output
    sent to a comms channel, not just left sitting on the Tasks tab). Only
    for a successful run with real output — a failed run's error stays on
    the Tasks tab rather than DMing a stack trace. Queued in the outbox
    (core/outbox.py), which retries a failed send and shows it on the run
    (David's ask 2026-09-02: a failed delivery must be visible, not just a log
    line). The outbox id, or None when no channel is set. The key is the
    task and the run's start, so the same run is never queued twice."""
    channel_id = task.get("deliver_to_channel")
    if not channel_id or not output:
        return None
    stored = task_service.get_task(task["id"]) or task
    key = f"task:{task['id']}:{stored.get('run_started_at')}:{channel_id}"
    return outbox.enqueue(key, channel_id, f"**{task['name']}**\n{output}", label=task["name"])


def _task_brain(task: dict):
    """The brain a task or card runs on: the model endpoint it names, or
    Claude through the CLI when it names none (every task's behaviour before
    endpoint_id existed). Detached from any chat, and never admin, same as
    task runs always were. An agent's work runs on the agent's model, with
    its tools and its inbox as the permission surface."""
    agent = agent_service.get(task.get("agent_id"))
    endpoint_id = agent["endpoint_id"] if agent else task.get("endpoint_id")
    if not endpoint_id:
        if agent:
            return Brain(agent_id=agent["id"], integration_ids=agent.get("integration_ids"))
        return Brain()
    from core import model_endpoints
    from services.chat_service import _build_brain
    endpoint = model_endpoints.get_endpoint(endpoint_id)
    if endpoint is None:
        where = f"{agent['name']}'s page" if agent else "Tasks"
        raise ValueError(f"the model this runs on has been removed; pick another on {where}")
    return _build_brain(endpoint, session_id=None, is_admin=False, agent_id=agent["id"] if agent else None)


def task_endpoint_id(task: dict) -> Optional[str]:
    """The model connection a task's usage counts toward on Home: the
    agent's or the task's own. A task that names none runs on Claude through
    the CLI (_task_brain), which is the one Claude Code connection when there
    is exactly one; with none or several, which one it was is unknown, and
    nothing is counted rather than guessed."""
    agent = agent_service.get(task.get("agent_id"))
    endpoint_id = agent["endpoint_id"] if agent else task.get("endpoint_id")
    if endpoint_id:
        return endpoint_id
    from core import model_endpoints
    claude = [e for e in model_endpoints.list_endpoints() if e.get("kind") == "claude_cli"]
    return claude[0]["id"] if len(claude) == 1 else None


async def complete(brain, prompt: str, task: dict, surface: str) -> str:
    """One task, card or goal run through the run contract (core/runs.py):
    the reply as run_turn gave it, and the run's usage - every provider call
    - counted on Home, which task runs never were before 2026-10-05. A run
    that was stopped or failed still counts what it used (2026-10-06)."""
    endpoint_id = task_endpoint_id(task)
    tally = runs.Tally()
    try:
        outcome = await runs.complete(brain, prompt, runs.RunContext(surface, endpoint_id=endpoint_id,
                                                                     task_id=task.get("id"),
                                                                     agent_id=task.get("agent_id"),
                                                                     model=getattr(brain, "model", None)),
                                      tally=tally)
    finally:
        if endpoint_id and tally.usage is not None:
            try:
                token_usage.record_usage(endpoint_id, tally.usage)
            except Exception:
                logger.exception("record_usage failed for endpoint %s", endpoint_id)
    return outcome.text


# -- starting and stopping runs ---------------------------------------------------

def _lock(task_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    held = _locks.get(task_id)
    if held is None or held[0] is not loop:
        held = _locks[task_id] = (loop, asyncio.Lock())
    return held[1]


def run_in_progress(task_id: str) -> bool:
    return task_id in _running


async def start_run(task: dict, source: str = "manual", wait: bool = False) -> bool:
    """Run a task, card or goal now, unless that task is already running:
    then False straight away, or with wait=True, run once the current run
    has finished (a trigger's event or an answer must not be lost). source:
    "schedule" (claims the occurrence: the schedule moves on first), or
    "manual", "trigger", "answer" (the schedule is left alone). Returns True
    once this run has ended, however it ended."""
    lock = _lock(task["id"])
    if lock.locked() and not wait:
        return False
    async with lock:
        if source == "schedule" and task.get("schedule_kind") != "card":
            # The occurrence is claimed here, before anything can await: the
            # schedule moves on now, so neither a crash mid-run nor a second
            # pass of the loop can run it again.
            if task_service.claim_occurrence(task["id"]) is None:
                return True  # gone, turned off, or no longer due
        work = asyncio.get_running_loop().create_task(_run_task(task, source))
        _running[task["id"]] = work
        try:
            # wait() rather than awaiting the work: stopping it cancels the
            # work, not whoever started it (a Run now request, say).
            await asyncio.wait({work})
        finally:
            _running.pop(task["id"], None)
        if work.cancelled():
            task_service.record_stopped(task["id"])
            events.emit("task.stopped", f"{task['name']} was stopped", task_id=task["id"])
            logger.info("'%s' (%s) was stopped", task["name"], task["id"])
        elif work.exception() is not None:
            # Each run records its own failures; this is a bug in that path.
            logger.error("'%s' (%s): the run raised", task["name"], task["id"], exc_info=work.exception())
    return True


def stop_run(task_id: str) -> bool:
    """Stop a running task, card or goal. False when it is not running."""
    work = _running.get(task_id)
    if work is None or work.done():
        return False
    work.cancel()
    return True


def start_in_background(task: dict, source: str) -> None:
    """A run started by an event (a trigger, an answer) rather than by a
    person waiting on it: in the background, after any run of the same task
    already going."""
    asyncio.get_running_loop().create_task(start_run(task, source, wait=True))


async def _run_task(task: dict, source: str = "manual") -> None:
    # Lines logged while the task runs name it (core/logs.py).
    tag = log_files.set_log_tag(f"task:{task['id']}")
    try:
        await _run_task_tagged(task, source)
    finally:
        log_files.reset_log_tag(tag)


async def _run_card(card: dict) -> None:
    """Run a claimed card once: its result goes to Review; a failure goes
    back to Ready for a retry, or to Blocked when out of attempts."""
    brain = None
    agent = agent_service.get(card.get("agent_id"))
    if agent:
        agent_service.begin_run(agent["id"], card_id=card["id"])
    try:
        brain = _task_brain(card)
        await brain.connect()
        prompt = task_service.card_prompt(card)
        if agent:
            prompt = agent_service.run_prompt(agent, prompt)
        async with file_checkpoints.around_turn(f"card:{card['id']}"):
            output = await complete(brain, prompt, card, "card")
        task_service.finish_card(card["id"], output)
        events.emit("card.review", f"{card['name']} is ready for review", task_id=card["id"])
        if agent:
            agent_service.notify_review(agent["id"], card)
        logger.info("card '%s' (%s) finished; waiting for review", card["name"], card["id"])
    except Exception as e:
        failed = task_service.fail_card(card["id"], str(e) or type(e).__name__)
        blocked = failed["status"] == "blocked"
        events.emit("card.blocked" if blocked else "card.retry",
                    f"{card['name']} {'is blocked' if blocked else 'failed and will retry'}: {e}",
                    level="error" if blocked else "warning", task_id=card["id"])
        logger.exception("card '%s' (%s) failed (attempt %s)", card["name"], card["id"], card["attempts"])
    finally:
        if agent:
            agent_service.end_run(agent["id"])
        if brain is not None:
            await brain.disconnect()


async def dispatch_cards() -> Optional[dict]:
    """One pass of the work board: recover runs that were lost, then run the
    oldest Ready card whose dependencies are Done. Returns the card run."""
    for card in task_service.reclaim_stale_cards(running=set(_running)):
        logger.warning("card '%s' (%s): its run was lost; now %s", card["name"], card["id"], card["status"])
    card = task_service.claim_next_card(eligible=agent_may_run)
    if card is not None:
        await start_run(card, "schedule")
    return card


def agent_may_run(task: dict) -> bool:
    """Ordinary work always may; an agent's only while it is on and under
    its daily run cap."""
    return not task.get("agent_id") or agent_service.can_run(task["agent_id"])[0]


async def _run_goal(task: dict, agent: dict) -> None:
    """One check of an agent's standing goal. The reply is its report unless
    it is [SILENT] (nothing worth the person's attention); either way the run
    is in the history."""
    agent_service.begin_run(agent["id"], task_id=task["id"])
    brain = None
    try:
        brain = _task_brain(task)
        await brain.connect()
        async with file_checkpoints.around_turn(f"task:{task['id']}"):
            output = await complete(brain, agent_service.run_prompt(agent, task["prompt"], goal=task), task, "goal")
        task_service.record_run(task["id"], output=output)
        if task.get("report_when") == "always" or not is_silent(output):
            item = agent_service.add_item(agent["id"], "report", task["name"], output)
            agent_service.notify(item)
        events.emit("task.run", f"{agent['name']} checked \"{task['name']}\"", task_id=task["id"])
    except Exception as e:
        task_service.record_run(task["id"], output="", error=str(e))
        events.emit("task.failed", f"{agent['name']}'s goal \"{task['name']}\" failed: {e}", level="error",
                    task_id=task["id"])
        logger.exception("agent goal '%s' (%s) failed", task["name"], task["id"])
    finally:
        agent_service.end_run(agent["id"])
        if brain is not None:
            await brain.disconnect()


async def _run_task_tagged(task: dict, source: str) -> None:
    if task.get("schedule_kind") == "card":
        await _run_card(task)
        return
    if source != "schedule":  # a scheduled run's start was stamped by its claim
        task_service.mark_started(task["id"], source)
    agent = agent_service.get(task.get("agent_id"))
    if agent:
        await _run_goal(task, agent)
        return
    builtin_id = task.get("builtin_action")
    if builtin_id:
        await _run_builtin_task(task, builtin_id)
        return

    brain = None
    try:
        brain = _task_brain(task)
        await brain.connect()
        async with file_checkpoints.around_turn(f"task:{task['id']}"):
            output = await complete(brain, task["prompt"], task, "task")
        delivery_id = _deliver(task, output)
        task_service.record_run(task["id"], output=output, delivery_id=delivery_id)
        events.emit("task.run", f"{task['name']} ran successfully", task_id=task["id"])
        logger.info("task '%s' (%s) ran successfully", task["name"], task["id"])
    except Exception as e:
        task_service.record_run(task["id"], output="", error=str(e))
        events.emit("task.failed", f"{task['name']} failed: {e}", level="error", task_id=task["id"])
        logger.exception("task '%s' (%s) failed", task["name"], task["id"])
    finally:
        if brain is not None:
            await brain.disconnect()


async def _run_builtin_task(task: dict, builtin_id: str) -> None:
    """Built-in tasks (David's ask 2026-08-31, see core/builtin_tasks.py)
    take a different execution path than a normal prompt task: "action" kind
    runs a plain Python function with no model call at all; "llm" kind
    builds a fresh, data-grounded prompt at RUN time (not creation time, so
    a recurring Daily Brief always reflects today's real data) and runs it
    through Brain same as any other task."""
    defn = BUILTIN_TASKS.get(builtin_id)
    if defn is None:
        task_service.record_run(task["id"], output="", error=f"unknown builtin action: {builtin_id}")
        return
    try:
        if defn["kind"] == "action":
            output = await defn["run"]()
        else:
            prompt = await defn["build_prompt"]()
            brain = _task_brain(task)
            try:
                await brain.connect()
                async with file_checkpoints.around_turn(f"task:{task['id']}"):
                    output = await complete(brain, prompt, task, "task")
            finally:
                await brain.disconnect()
        delivery_id = _deliver(task, output)
        task_service.record_run(task["id"], output=output, delivery_id=delivery_id)
        events.emit("task.run", f"{task['name']} ran successfully", task_id=task["id"])
        logger.info("builtin task '%s' (%s) ran successfully", task["name"], task["id"])
    except Exception as e:
        task_service.record_run(task["id"], output="", error=str(e))
        events.emit("task.failed", f"{task['name']} failed: {e}", level="error", task_id=task["id"])
        logger.exception("builtin task '%s' (%s) failed", task["name"], task["id"])


# -- the loop -------------------------------------------------------------------------

async def _run_due() -> None:
    """The scheduled lane: every task due now, one after another."""
    for task in task_service.due_tasks():
        if not task_service.is_due(task["id"]):
            continue  # claimed or changed since the list was read
        if not agent_may_run(task):
            # Off or out of runs today: this occurrence is skipped, not
            # queued, so turning it back on never floods runs.
            task_service.skip_occurrence(task["id"])
            continue
        if not await start_run(task, "schedule") and task_service.is_due(task["id"]):
            # Already running (a Run now, say): this occurrence is covered.
            task_service.skip_occurrence(task["id"])
            logger.info("task '%s' (%s) was due while already running; skipped to its next time",
                        task["name"], task["id"])


def _lane_free(name: str) -> bool:
    lane = _lanes[name]
    return lane is None or lane.done()


async def _tick() -> None:
    """One pass of the loop: start a lane that is free and has work, and send
    the outbox's due messages. Never waits on a run."""
    loop = asyncio.get_running_loop()
    if _lane_free("schedule") and task_service.due_tasks():
        _lanes["schedule"] = loop.create_task(_run_due())
    if _lane_free("card"):
        # One card per pass (see dispatch_cards).
        _lanes["card"] = loop.create_task(dispatch_cards())
    await outbox.send_due()


async def _poll_loop() -> None:
    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("task_scheduler poll loop iteration failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _when(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%a %b %d, %H:%M")
    except ValueError:
        return iso


def _report_lost(task: dict) -> None:
    """A run cut off by JARVIS closing: said in the feed, and on the task's
    channel when it has one, since it will not run again by itself."""
    after = (f"It runs again at {_when(task.get('next_run_at'))}" if task.get("enabled") and task.get("next_run_at")
             else "It won't run again by itself")
    text = f"{task['name']} was cut off when JARVIS closed. {after}; use Run now on the Tasks tab to redo it."
    logger.warning("task '%s' (%s): %s", task["name"], task["id"], text)
    events.emit("task.lost", text, level="warning", task_id=task["id"])
    channel = task.get("deliver_to_channel")
    if channel:
        last = task_service.list_runs(task["id"])[0]
        outbox.enqueue(f"lost:{task['id']}:{last.get('started_at')}:{channel}", channel, f"**{task['name']}**\n{text}",
                       label=task["name"])


def start() -> None:
    global _loop_task
    if _loop_task is None:
        outbox.recover()
        for task in task_service.recover_interrupted_runs():
            _report_lost(task)
        _loop_task = asyncio.create_task(_poll_loop())
        logger.info("task_scheduler started (poll every %ss)", POLL_INTERVAL_SECONDS)


def stop() -> None:
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


def is_running() -> bool:
    """Live scheduler health for /api/system/status — done (crashed/cancelled)
    counts as not running, not just never-started."""
    return _loop_task is not None and not _loop_task.done()
