"""Helpers: short-lived, read-only jobs a chat, task, card or goal hands out
(roadmap phase 5, 2026-10-06; spec: the vault note "Helpers - Phase 5 (Build
Spec)"). After Hermes Agent's delegate_task (tools/delegate_tool.py, MIT).

The `delegate` tool (core/tool_registry.py) gives this module 1 to 3 jobs.
Each runs as its own model run on the helper connection - one chosen in
Settings, never by the model; by default the first local one, so helpers
cost nothing - with only the read tools and the sandboxed browser
(tool_registry.helper_tool), and returns its findings. Bounds:

- MAX_PER_CALL jobs per call, MAX_RUNNING helpers at once across the app
  (the rest wait their turn);
- HELPER_SECONDS and HELPER_TOKENS each; past either a helper is stopped and
  keeps what it had;
- one level deep: a helper has no `delegate`.

The tool waits up to WAIT_SECONDS, under the 180 seconds Claude Code and
Codex wait for a tool to answer; helpers still working after that carry on,
and `helper_results` collects them later. Stopping the parent (a chat's Stop,
a task's) stops its helpers.

Every helper is a row in the session store's `helpers` table (schema v6), and
its run a `runs` row whose parent is the run that asked. A helper left
waiting or running when Kairos closed is marked lost at the next start and
never run again (as phase 4 does for scheduled runs); finished results stay
collectable. Findings always come back fenced as untrusted data, and taint
the turn that reads them.
"""
import asyncio
import logging
import time
import uuid
from typing import Optional

from core import runs, session_manager_store as store, token_usage
from core.untrusted import wrap_untrusted

logger = logging.getLogger(__name__)

MAX_PER_CALL = 3
MAX_RUNNING = 3
HELPER_SECONDS = 10 * 60
HELPER_TOKENS = 40_000
# Claude Code and Codex give a tool 180 seconds to answer.
WAIT_SECONDS = 150
RESULT_CHARS = 4000
GOAL_CHARS = 4000
CONTEXT_CHARS = 8000
KEEP = 2000
UNFINISHED = ("waiting", "running")
OFF = "off"
CHECK_SECONDS = 0.25

# One per event loop: an asyncio semaphore belongs to the loop it was used on.
_slots: dict = {}
# The helpers running for each parent (chat:<id>, task:<id>, ...), for stop_parent.
_active: dict[str, set] = {}


# -- which connection ---------------------------------------------------------------

def helper_endpoint() -> Optional[dict]:
    """The connection helpers run on: the one chosen in Settings, or by
    default the first local one. None when helpers are off, or the choice is
    gone or not a local or API connection (Claude and Codex keep their own
    subagents; their built-in tools can't be limited to reading here)."""
    from core import model_endpoints, settings as settings_store
    choice = settings_store.get_setting("helper_endpoint_id")
    if choice == OFF:
        return None
    if choice:
        endpoint = model_endpoints.get_endpoint(choice)
        return endpoint if endpoint and endpoint.get("kind") in ("local", "api") else None
    return next((e for e in model_endpoints.list_endpoints() if e.get("kind") == "local"), None)


def _brain(endpoint: dict):
    from core import model_catalog, model_endpoints
    from core.external_brain import ExternalBrain
    base_url, model, api_key, num_ctx = model_endpoints.resolve_runtime(endpoint["id"])
    capacity = model_catalog.context_capacity("api", model) if endpoint["kind"] == "api" else None
    window = num_ctx if endpoint["kind"] == "local" else (capacity or {}).get("window")
    return ExternalBrain(base_url, model, api_key, num_ctx=num_ctx, endpoint_id=endpoint["id"], window=window,
                         helper=True)


# -- who asked ----------------------------------------------------------------------

def parent_key(ctx) -> str:
    """The chat, task or agent a tool call belongs to."""
    if ctx.session_id:
        return f"chat:{ctx.session_id}"
    current = runs.CURRENT.get()
    if current is not None and current.task_id:
        return f"task:{current.task_id}"
    if ctx.agent_id:
        return f"agent:{ctx.agent_id}"
    return "detached"


def stop_parent(parent: str) -> int:
    """Stop every helper still working for this parent; how many."""
    working = [t for t in _active.get(parent, ()) if not t.done()]
    for task in working:
        task.cancel()
    return len(working)


# -- the store ----------------------------------------------------------------------

def _rows(where: str, params: tuple) -> list[dict]:
    with store.transaction() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM helpers WHERE {where} ORDER BY created_at, rowid", params)]


def _update(helper_id: str, **fields) -> None:
    with store.transaction() as conn:
        conn.execute(f"UPDATE helpers SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), helper_id))


def recover() -> int:
    """At startup nothing is running: a helper still waiting or running was
    cut off by Kairos closing. Marked lost, never run again."""
    with store.transaction() as conn:
        cut = conn.execute("UPDATE helpers SET status = 'lost', ended_at = ?, "
                           "error = 'Kairos closed while it worked; it was not run again' "
                           "WHERE status IN ('waiting', 'running')", (time.time(),)).rowcount
    if cut:
        logger.warning("helpers: %d helper(s) were cut off by Kairos closing; marked lost", cut)
    return cut


def _prune(conn) -> None:
    conn.execute("DELETE FROM helpers WHERE id IN (SELECT id FROM helpers WHERE status NOT IN ('waiting', 'running') "
                 "ORDER BY created_at DESC LIMIT -1 OFFSET ?)", (KEEP,))


# -- running ------------------------------------------------------------------------

def _slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    for old in [l for l in _slots if l is not loop and l.is_closed()]:
        del _slots[old]
    return _slots.setdefault(loop, asyncio.Semaphore(MAX_RUNNING))


def _prompt(row: dict) -> str:
    return row["goal"] + (f"\n\n[Context from the conversation that asked:]\n{row['context']}" if row["context"] else "")


async def _run_one(row: dict, endpoint: dict, parent_run_id: Optional[str], session_id: Optional[str]) -> None:
    """One helper, start to finish; its row says how it ended."""
    status, text, error, tally, started = "stopped", "", None, runs.Tally(), None
    brain = work = None
    try:
        async with _slot():
            started = time.time()
            _update(row["id"], status="running", started_at=started)
            brain = _brain(endpoint)
            context = runs.RunContext("helper", parent_run_id=parent_run_id, session_id=session_id,
                                      endpoint_id=endpoint["id"], model_kind=endpoint.get("kind"), model=brain.model,
                                      budget_tokens=HELPER_TOKENS, deadline=started + HELPER_SECONDS)
            work = asyncio.get_running_loop().create_task(runs.complete(brain, _prompt(row), context, tally=tally))
            limit = None
            while not work.done():
                await asyncio.wait({work}, timeout=CHECK_SECONDS)
                if work.done():
                    break
                if tally.usage is not None and tally.usage.total_tokens > HELPER_TOKENS:
                    limit = "over budget"
                elif time.time() > started + HELPER_SECONDS:
                    limit = "timed out"
                if limit:
                    work.cancel()
                    await asyncio.wait({work})
                    break
            if limit:
                status, text = limit, "".join(tally.parts).strip()
                error = (f"stopped at {HELPER_TOKENS:,} tokens" if limit == "over budget"
                         else f"stopped after {HELPER_SECONDS // 60} minutes")
            elif work.exception() is not None:
                status, error = "failed", f"{type(work.exception()).__name__}: {work.exception()}"
            else:
                status, text = "done", work.result().text
    except asyncio.CancelledError:
        # Stopped with its parent; what it had so far is kept.
        if work is not None and not work.done():
            work.cancel()
            await asyncio.wait({work})
        status, text = "stopped", "".join(tally.parts).strip()
        raise
    except Exception as e:  # building the brain, say: this helper fails, its batch goes on
        status, error = "failed", f"{type(e).__name__}: {e}"
        logger.exception("helper %s failed", row["id"])
    finally:
        usage = tally.usage
        if usage is not None:
            try:
                token_usage.record_usage(endpoint["id"], usage)
            except Exception:
                logger.exception("record_usage failed for helper %s", row["id"])
        _update(row["id"], status=status, result=text or None, error=error,
                tokens=usage.total_tokens if usage else None, ended_at=time.time())
        if brain is not None:
            await brain.disconnect()


def _jobs(jobs) -> list[dict]:
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_PER_CALL:
        raise ValueError(f"give 1 to {MAX_PER_CALL} jobs")
    clean = []
    for job in jobs:
        goal = (job or {}).get("goal") if isinstance(job, dict) else None
        context = (job or {}).get("context") if isinstance(job, dict) else None
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("every job needs a goal")
        if context is not None and not isinstance(context, str):
            raise ValueError("a job's context is text")
        if len(goal) > GOAL_CHARS or len(context or "") > CONTEXT_CHARS:
            raise ValueError(f"keep a goal under {GOAL_CHARS:,} and its context under {CONTEXT_CHARS:,} characters")
        clean.append({"goal": goal.strip(), "context": (context or "").strip()})
    return clean


async def delegate(jobs, ctx) -> str:
    """The `delegate` tool: start the helpers, wait for them up to
    WAIT_SECONDS, and say what they found."""
    try:
        clean = _jobs(jobs)
    except ValueError as e:
        return f"Not run: {e}."
    endpoint = helper_endpoint()
    if endpoint is None:
        return ("Not run: helpers are off. Choose a local or API model for them in Settings > Added Models > "
                "Helpers. Do the work yourself for now.")
    parent = parent_key(ctx)
    current = runs.current_for(ctx.session_id)
    batch_id = uuid.uuid4().hex[:10]
    now = time.time()
    rows = [{"id": uuid.uuid4().hex[:12], "batch_id": batch_id, "parent": parent,
             "parent_run_id": current.run_id if current else None, "goal": job["goal"], "context": job["context"],
             "endpoint_id": endpoint["id"], "model": endpoint.get("model"), "status": "waiting", "created_at": now + i / 1000}
            for i, job in enumerate(clean)]
    with store.transaction() as conn:
        for row in rows:
            conn.execute(f"INSERT INTO helpers ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                         tuple(row.values()))
        _prune(conn)
    loop = asyncio.get_running_loop()
    tasks = [loop.create_task(_run_one(row, endpoint, row["parent_run_id"], ctx.session_id)) for row in rows]
    working = _active.setdefault(parent, set())
    for task in tasks:
        working.add(task)
        task.add_done_callback(working.discard)
    try:
        await asyncio.wait(tasks, timeout=WAIT_SECONDS)
    except asyncio.CancelledError:
        # The turn that asked was stopped: so are its helpers.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    logger.info("helpers: batch %s for %s on %s: %s", batch_id, parent, endpoint.get("name"),
                ", ".join(r["status"] for r in _rows("batch_id = ?", (batch_id,))))
    return _report(batch_id, ctx)


# -- reading results ------------------------------------------------------------------

def _report(batch_id: str, ctx) -> str:
    rows = _rows("batch_id = ?", (batch_id,))
    if ctx.turn_taint is not None and any(r["result"] for r in rows):
        # A helper's findings are a model's account of what it read.
        ctx.turn_taint.mark("helper results")
    lines = [f"Helpers' results (batch {batch_id}):"]
    for n, row in enumerate(rows, 1):
        took = f"{round(row['ended_at'] - row['started_at'])}s" if row["ended_at"] and row["started_at"] else ""
        facts = [row["status"], f"{row['tokens']:,} tokens" if row["tokens"] else "", took]
        lines.append(f"\n{n}. {row['goal'][:120]} — {' · '.join(f for f in facts if f)}")
        if row["error"]:
            lines.append(f"({row['error']})")
        if row["result"]:
            text = row["result"]
            if len(text) > RESULT_CHARS:
                text = text[:RESULT_CHARS] + f"\n[... cut at {RESULT_CHARS:,} characters]"
            lines.append(wrap_untrusted("a helper's findings", text))
    still = [r for r in rows if r["status"] in UNFINISHED]
    if still:
        lines.append(f"\n{len(still)} helper(s) still working. Call helper_results with batch_id \"{batch_id}\" "
                     "to collect them later.")
    return "\n".join(lines)


def results_text(batch_id: Optional[str], ctx) -> str:
    """The `helper_results` tool: one batch of this conversation's helpers,
    or its latest."""
    parent = parent_key(ctx)
    if batch_id:
        if not _rows("batch_id = ? AND parent = ?", (batch_id, parent)):
            return "No helpers with that batch id in this conversation."
        return _report(batch_id, ctx)
    with store.transaction() as conn:
        row = conn.execute("SELECT batch_id FROM helpers WHERE parent = ? ORDER BY created_at DESC LIMIT 1",
                           (parent,)).fetchone()
    if row is None:
        return "This conversation has not handed any work to helpers."
    return _report(row["batch_id"], ctx)


def batch(batch_id: str) -> list[dict]:
    return _rows("batch_id = ?", (batch_id,))


def batch_rows_for_run(run_id: str) -> list[dict]:
    """The helpers a run handed work to (its timeline, roadmap phase 8)."""
    return _rows("parent_run_id = ?", (run_id,))
