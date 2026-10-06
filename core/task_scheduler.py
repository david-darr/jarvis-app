"""Background loop that executes due scheduled tasks. Started once at app
startup (see app.py's lifespan), stopped at shutdown.

Each due task gets its own short-lived Brain (connect, run the task's prompt
once, disconnect) rather than reusing a long-lived connection — task runs are
independent one-shot executions, not a persisted conversation the way chat
sessions are.
"""
import asyncio
import logging
from typing import Optional

from core import events, file_checkpoints, logs as log_files, runs, token_usage
from core.brain import Brain
from core.builtin_tasks import BUILTIN_TASKS
from core.channels import registry as channel_registry
from services.agent_service import agent_service, is_silent
from services.task_service import task_service

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15

_loop_task: asyncio.Task | None = None


async def _deliver(task: dict, output: str) -> Optional[bool]:
    """Settings > Channels delivery (David's ask 2026-08-31: task output
    sent to a comms channel, not just left sitting on the Tasks tab). Only
    fires on a successful run with real output — a failed run's error stays
    on the Tasks tab rather than DMing a stack trace. Returns None when no
    channel is configured (nothing attempted), else the send's own success
    bool — the caller stores this on the run record so a failed delivery is
    visible on the Tasks tab, not just this log line (David's ask 2026-09-02,
    found live: a Discord bot with no allowed_user_id set failed delivery
    with zero visibility anywhere but here)."""
    channel_id = task.get("deliver_to_channel")
    if not channel_id or not output:
        return None
    delivered = await channel_registry.send_to_channel(channel_id, f"**{task['name']}**\n{output}")
    if not delivered:
        logger.warning("task '%s' (%s): delivery to channel '%s' failed", task["name"], task["id"], channel_id)
        events.emit("task.delivery_failed", f"{task['name']}: delivery to {channel_id} failed", level="error",
                    task_id=task["id"], channel=channel_id)
    return delivered


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
    - counted on Home, which task runs never were before 2026-10-05."""
    endpoint_id = task_endpoint_id(task)
    outcome = await runs.complete(brain, prompt, runs.RunContext(surface, endpoint_id=endpoint_id,
                                                                 agent_id=task.get("agent_id")))
    if endpoint_id:
        try:
            token_usage.record_usage(endpoint_id, outcome.usage)
        except Exception:
            logger.exception("record_usage failed for endpoint %s", endpoint_id)
    return outcome.text


async def _run_task(task: dict) -> None:
    # Lines logged while the task runs name it (core/logs.py).
    tag = log_files.set_log_tag(f"task:{task['id']}")
    try:
        await _run_task_tagged(task)
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
    for card in task_service.reclaim_stale_cards():
        logger.warning("card '%s' (%s): its run was lost; now %s", card["name"], card["id"], card["status"])
    card = task_service.claim_next_card(eligible=agent_may_run)
    if card is not None:
        await _run_task(card)
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


async def _run_task_tagged(task: dict) -> None:
    if task.get("schedule_kind") == "card":
        await _run_card(task)
        return
    task_service.mark_started(task["id"])
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
        delivered = await _deliver(task, output)
        task_service.record_run(task["id"], output=output, delivered=delivered)
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
        delivered = await _deliver(task, output)
        task_service.record_run(task["id"], output=output, delivered=delivered)
        events.emit("task.run", f"{task['name']} ran successfully", task_id=task["id"])
        logger.info("builtin task '%s' (%s) ran successfully", task["name"], task["id"])
    except Exception as e:
        task_service.record_run(task["id"], output="", error=str(e))
        events.emit("task.failed", f"{task['name']} failed: {e}", level="error", task_id=task["id"])
        logger.exception("builtin task '%s' (%s) failed", task["name"], task["id"])


async def _poll_loop() -> None:
    while True:
        try:
            for task in task_service.due_tasks():
                if not agent_may_run(task):
                    # Off or out of runs today: this occurrence is skipped,
                    # not queued, so turning it back on never floods runs.
                    task_service.skip_occurrence(task["id"])
                    continue
                await _run_task(task)
            # One card per pass (see dispatch_cards): scheduled tasks never
            # wait behind a whole board of work.
            await dispatch_cards()
        except Exception:
            logger.exception("task_scheduler poll loop iteration failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def start() -> None:
    global _loop_task
    if _loop_task is None:
        for task in task_service.recover_interrupted_runs():
            logger.warning("task '%s' (%s): its last run was cut off by the app closing", task["name"], task["id"])
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
