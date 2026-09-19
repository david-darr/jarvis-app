"""Owner-facing Swarm boundary. Importing it has no startup side effects."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path

from core import model_catalog, model_endpoints
from core.swarm import accounts, architect
from core.swarm.adapters import WorkerContext, build_worker, capability_for
from core.swarm.budget import BudgetLimit
from core.swarm.checkpoints import download_handoff
from core.swarm.models import Conflict, NotFound, PersistenceFault
from core.swarm.runtime import SwarmRuntime
from core.swarm.store import SwarmStore
from core.swarm.tools import ToolService

logger = logging.getLogger(__name__)

# A single step's dispatch ceiling, derived from the smallest allocation in
# play so one worker cannot spend a whole company's budget in one turn. This
# is a local bound on what Swarm will admit, not a claim about provider
# billing.
STEP_SHARE = 4
MIN_STEP_UNITS = 500
# How many cycles an autonomous company may open before it must stop and say
# so. Novelty alone must not keep a company spending: this is a backstop
# behind the lead's own judgement, the budgets, and the no-progress check.
MAX_CYCLES = 5


class SwarmService:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.store = None
        self.runtime = None
        self.fault = None
        self.lock = asyncio.Lock()
        self.cycles = {}

    async def start(self):
        try:
            self.store = SwarmStore(self.directory / "swarm.sqlite3")
            # Longer than the engine's default so a worker's own bounded
            # stop can answer inside it. Measured live: the Claude CLI does not
            # acknowledge an interrupt mid-turn at all, at 5 seconds or at 25,
            # so a pause takes the short path and records an honest unknown
            # rather than waiting for an acknowledgement that never comes.
            self.runtime = SwarmRuntime(self.store, self.directory / "systems", poll_interval=1, stop_timeout=12)
            await self.runtime.start()
        except Exception as exc:
            self.fault = exc
            logger.exception("Swarm unavailable; other app features remain available")

    def status(self, system_id=None):
        """Availability, and for a named company its own verdict.

        Callers pass a system id only after ownership has been checked, so a
        setup problem can never confirm that someone else's company exists.
        """
        fault = self.fault or (self.runtime and self.runtime.fault) or (self.store and self.store.fault)
        available = self.store is not None and self.runtime is not None and self.runtime.generation is not None and not fault
        if not available:
            return {"available": False, "execution_available": False, "code": "swarm_unavailable",
                    "detail": "Swarm storage or coordinator is unavailable. Check the backend log.",
                    "blockers": []}
        result = {"available": True, "execution_available": True, "code": "ready", "blockers": [],
                  "detail": "Workers have no shell, file or vault access. They can plan, research, review and write."}
        if system_id:
            problems = self.agent_blockers(system_id)
            if problems:
                result.update({"execution_available": False, "code": "setup_blocked",
                               "detail": " ".join(problems), "blockers": problems})
        return result

    # -- execution ---------------------------------------------------------

    @staticmethod
    def _resolved(endpoint_id):
        """Endpoint detail for one execution, key included and never stored.

        Resolution happens here, at the moment of use. Nothing returned by this
        method reaches the store, a transcript, an event or a handoff.
        """
        record = model_endpoints.get_endpoint(endpoint_id) if endpoint_id else None
        if record is None:
            return None
        base_url, model, api_key, num_ctx = model_endpoints.resolve_runtime(endpoint_id)
        return {"id": endpoint_id, "name": record.get("name") or endpoint_id,
                "kind": record.get("kind") or "api", "base_url": base_url, "model": model,
                "api_key": api_key, "num_ctx": num_ctx}

    @staticmethod
    def _connection(endpoint_id):
        """Identity and kind only, with nothing decrypted.

        This runs on every snapshot, and a snapshot refreshes on every event.
        Capability is decided by kind, so reaching for the key material each
        time would handle a credential thousands of times to answer a question
        that never needed it.
        """
        record = model_endpoints.get_endpoint(endpoint_id) if endpoint_id else None
        if record is None:
            return None
        return {"id": endpoint_id, "name": record.get("name") or endpoint_id,
                "kind": record.get("kind") or "api"}

    def agent_blockers(self, system_id):
        """Why this company cannot run, per teammate, in the owner's words."""
        problems = []
        for agent in self.store.team(system_id):
            if not agent["enabled"]:
                continue
            endpoint = self._connection(agent.get("endpoint_id"))
            if endpoint is None:
                problems.append(f"{agent['name']} has no model connection." if not agent.get("endpoint_id")
                                else f"{agent['name']}'s model connection no longer exists.")
                continue
            reason = capability_for(endpoint).blocked_reason()
            if reason:
                problems.append(f"{agent['name']} ({endpoint['name']}): {reason}")
        return problems

    async def draft_team(self, owner, description, endpoint_id):
        """Propose a roster for someone who knows the goal, not the shape.

        Deliberately outside the setup path: saving a company never calls a
        model, and this only runs when the owner asks for it and names the
        connection that should pay for it. What comes back is a proposal to
        edit - nothing is created, and no connection is assigned, because
        which model an agent may run on is a capability decision rather than
        a suggestion.
        """
        self.ready()
        endpoint = self._resolved(endpoint_id)
        if endpoint is None:
            raise NotFound("That model connection does not exist")
        draft = await architect.draft(endpoint, description)
        logger.info("swarm: drafted a team of %d for %s", len(draft["specialists"]), owner)
        return draft

    def account_for(self, endpoint_id):
        """The pool identity for a connection, for the setup path."""
        endpoint = self._resolved(endpoint_id)
        if endpoint is None:
            return None, None
        return accounts.account_key(endpoint), accounts.account_label(endpoint)

    def _step_units(self, system_id):
        smallest = None
        for agent in self.store.team(system_id):
            if not agent["enabled"]:
                continue
            row = self.store.db.execute("SELECT ceiling FROM budgets WHERE scope='agent' AND target=?",
                                        (agent["id"],)).fetchone()
            if row and (smallest is None or row[0] < smallest):
                smallest = row[0]
        return max(MIN_STEP_UNITS, (smallest or MIN_STEP_UNITS * STEP_SHARE) // STEP_SHARE)

    def _worker_factory(self, system_id):
        system = self.store.get_system(system_id)
        scratch = self.directory / "systems" / system_id / "work"
        scratch.mkdir(parents=True, exist_ok=True)

        def factory(task):
            agent = self.store.agent_config(task["agent_id"])
            endpoint = self._resolved(agent.get("endpoint_id"))
            if endpoint is None:
                raise PersistenceFault(f"{agent['name']} has no usable model connection")
            return build_worker(WorkerContext(
                endpoint=endpoint, agent=agent, system=system,
                tool_service=ToolService(self.store, is_lead=bool(agent["is_lead"])),
                scratch_dir=str(scratch), model=agent.get("model") or endpoint.get("model") or None,
                effort=agent.get("effort") or None, env={}))

        return factory

    FIRST_OBJECTIVE = "Read the owner's messages and the mission, then assign the work to your team."
    NEXT_OBJECTIVE = ("Review what this company has produced against its mission. If the mission is met, "
                      "call finish_mission. If useful work genuinely remains, assign it.")

    def _seed_run(self, system_id, objective=None):
        """Open a run and give the lead the first task. No model call here."""
        system = self.store.get_system(system_id)
        configuration = json.loads(system.get("configuration") or "{}")
        run = self.store.open_run(system_id)
        if run is None:
            limit = configuration.get("run_limit") or {"ceiling": 25000, "pause_percent": 80, "checkpoint_reserve": 0}
            run_id = self.store.create_run(system_id, system["mission"], BudgetLimit(**limit))
        else:
            run_id = run["id"]
        lead = next(agent for agent in self.store.team(system_id) if agent["is_lead"])
        open_lead_task = self.store.db.execute(
            "SELECT 1 FROM tasks WHERE agent_id=? AND state IN ('ready','running','review')", (lead["id"],)).fetchone()
        if open_lead_task:
            return run_id
        self.store.create_plan(system_id, run_id, lead["id"], [{
            "key": "lead",
            "agent_id": lead["id"],
            "objective": objective or self.FIRST_OBJECTIVE,
        }], command_id=f"seed:{run_id}")
        return run_id

    def _advance(self, system_id):
        """Deterministic between-stage step: send finished work to the lead.

        Returns True only when it actually created new work, so a lead that
        declines to review cannot make this loop forever.
        """
        waiting = self.store.tasks_in_review(system_id)
        if not waiting:
            return False
        team = self.store.team(system_id)
        lead = next(agent for agent in team if agent["is_lead"])
        # Nobody reviews the lead, so its own coordination tasks are closed
        # here on evidence taken from the database - the tasks its plan really
        # created, the decisions its review really recorded - never on the
        # model's word that it did something. Anything else it submits stays in
        # review for the owner to look at.
        closed = False
        for task in list(waiting):
            if task["agent_id"] != lead["id"]:
                continue
            result = json.loads(task["result"] or "{}")
            if result.get("status") == "planned":
                created = [task_id for task_id in (result.get("tasks") or {}).values()
                           if self.store.db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()]
                evidence = f"Plan recorded: {len(created)} task(s) exist in this run."
            elif result.get("status") == "reviewed":
                evidence = f"Review recorded: {len(result.get('decisions') or [])} decision(s)."
            elif result.get("status") == "concluded":
                evidence = "The lead ended the mission; its own record of that decision is the task's result."
            else:
                continue
            self.store.review_task(task["id"], lead["id"], accept=True, note=evidence)
            waiting.remove(task)
            closed = True
        if closed:
            return True
        # A blocked task is a question for the owner, not work awaiting review.
        # Counting the lead's own escalation as "the lead is busy" is what
        # wedged a real company: no further review task could ever be created,
        # and everything stopped in silence. Specialists' blockers stay
        # reviewable, so the lead still gets one chance to reassign or work
        # around one - the digest below makes it exactly one.
        def status_of(task):
            try:
                return json.loads(task["result"] or "{}").get("status")
            except ValueError:
                return None

        # A lead step that ran out of turns did not fail and was not judged -
        # it simply did not get to finish. Found on David's run, where the
        # lead spent its turns messaging two teammates and then hit the
        # ceiling. One more attempt is fair; a second would be a loop, so
        # after that it stops counting as the lead being busy and the company
        # concludes instead of wedging.
        for task in list(waiting):
            if task["agent_id"] != lead["id"] or status_of(task) != "incomplete":
                continue
            if self.store.attempt_count(task["id"]) < 2:
                self.store.retry_task(task["id"], json.loads(task["result"] or "{}").get("reason", ""))
                return True
            waiting.remove(task)

        waiting = [task for task in waiting
                   if not (task["agent_id"] == lead["id"] and status_of(task) == "blocked")]
        if any(task["agent_id"] == lead["id"] for task in waiting):
            return False
        if not waiting:
            return False
        run = self.store.open_run(system_id)
        if run is None:
            return False
        # The revision, not just the id: a task that was sent back and
        # resubmitted is new work to review. Keying on ids alone replayed the
        # first review task, which was already done, and the cycle stopped
        # with finished work still waiting - seen on the live run.
        digest = hashlib.sha256(",".join(sorted(f"{task['id']}@{task['revision']}" for task in waiting)).encode()).hexdigest()[:16]
        created = self.store.create_plan(system_id, run["id"], lead["id"], [{
            "key": "review",
            "agent_id": lead["id"],
            "objective": f"Review the {len(waiting)} submitted task(s) and decide on each one.",
        }], command_id=f"review:{digest}")
        task_id = created.get("review")
        return bool(task_id) and self.store.get_task(task_id)["state"] == "ready"

    @staticmethod
    def _request_text(blocked):
        """What the team is actually asking for, in its own words."""
        asks = []
        for task in blocked:
            try:
                result = json.loads(task["result"] or "{}")
            except ValueError:
                continue
            asks.append((result.get("needs") or result.get("reason") or "").strip())
        joined = " ".join(ask for ask in asks if ask)
        return joined[:4000] or "The team stopped and could not say what it needs."

    def _continue_reason(self, system_id):
        """Whether to open another cycle, and if not, why not.

        Every answer is read back out of persisted state. A company stops
        because its lead said the mission was met, because a ceiling was
        reached, or because the last cycle produced nothing accepted - never
        because a loop quietly ran out.
        """
        system = self.store.get_system(system_id)
        if json.loads(system.get("configuration") or "{}").get("mode") != "autonomous":
            return False, None           # guided: one cycle per Start, as before
        if self.store.mission_concluded(system_id):
            return False, None           # already recorded
        if system["state"] != "idle":
            if system["state"] != "active":
                return False, None       # paused or stopped record their own reasons
            # Claimable work, not nominally ready work. A task whose dependency
            # will never finish stays ready for ever, which is exactly how a
            # wedged company looked busy instead of stuck.
            running = self.store.db.execute(
                "SELECT 1 FROM tasks WHERE system_id=? AND state='running'", (system_id,)).fetchone()
            if running or self.store.eligible_tasks(system_id):
                return False, None
            blocked = self.store.blocked_for_owner(system_id)
            if blocked:
                return False, ("needs_owner", self._request_text(blocked))
            return False, ("stalled", "A cycle ended without the lead assigning work or finishing the mission.")
        finished = self.store.db.execute(
            """SELECT result FROM tasks WHERE system_id=? AND result IS NOT NULL
               AND json_valid(result) AND json_extract(result, '$.status')='concluded'""",
            (system_id,)).fetchone()
        if finished:
            return False, ("mission_complete", json.loads(finished[0]).get("summary"))
        if self.store.run_count(system_id) >= MAX_CYCLES:
            return False, ("cycle_limit", f"Reached the {MAX_CYCLES}-cycle ceiling for one start.")
        return True, None

    async def _cycle(self, system_id):
        try:
            while True:
                await self.runtime.run_cycle(system_id, self._worker_factory(system_id),
                                             max_units=self._step_units(system_id), advance=self._advance)
                keep_going, ending = self._continue_reason(system_id)
                if ending:
                    reason, summary = ending
                    self.store.conclude_mission(system_id, reason=reason, summary=summary)
                if not keep_going:
                    break
                self._seed_run(system_id, self.NEXT_OBJECTIVE)
        except Exception:
            logger.exception("Swarm cycle ended with an error; state is preserved")
        finally:
            self.cycles.pop(system_id, None)

    def _dispatch(self, system_id):
        if system_id in self.cycles:
            return
        self.cycles[system_id] = asyncio.create_task(self._cycle(system_id))

    def ready(self):
        if not self.status()["available"]:
            raise PersistenceFault("Swarm is unavailable. Check the backend log.")

    def _member(self, member, pool_limit):
        """Attach the server's own account verdict to one teammate.

        A request may name a connection; it may never name the account that
        connection belongs to. That is derived here, so a client cannot claim
        a separate allowance for a shared account by asserting one.
        """
        member = {**member, "account_key": None, "account_label": None, "pool_limit": pool_limit}
        endpoint_id = member.get("endpoint_id")
        if not endpoint_id:
            return member
        endpoint = self._resolved(endpoint_id)
        if endpoint is None:
            raise ValueError(f"{member['name']}'s model connection does not exist")
        if member.get("effort") and not model_catalog.validate_effort(
                endpoint["kind"], member.get("model") or endpoint.get("model"), member["effort"]):
            raise ValueError(f"{member['name']}'s reasoning effort is not one this connection supports")
        member["account_key"] = accounts.account_key(endpoint)
        member["account_label"] = accounts.account_label(endpoint)
        return member

    def _prepare(self, data):
        pool_limit = data.get("pool_limit")
        return {**data,
                "lead": self._member(data["lead"], pool_limit),
                "specialists": [self._member(member, pool_limit) for member in data["specialists"]]}

    async def create(self, owner, data, command_id):
        async with self.lock:
            self.ready()
            return self.store.owner_create(owner, self._prepare(data), command_id)

    async def update(self, owner, system_id, data, command_id, revision):
        async with self.lock:
            self.ready()
            return self.store.owner_update(owner, system_id, self._prepare(data), command_id, revision)

    async def message(self, owner, system_id, body, command_id):
        """Send the lead an idea - or the answer a stopped team was waiting for.

        A company that stopped because it needed something from its owner is
        waiting on exactly this. Answering returns the held work to ready and
        starts the cycle again, so the next attempt finds the answer in its
        inbox rather than the owner having to know to press Start.
        """
        async with self.lock:
            self.ready()
            result = self.store.owner_message(owner, system_id, body, command_id)
            resumed = False
            if self.store.blocked_for_owner(system_id):
                resumed = bool(self.store.answer_blockers(system_id, body))
            if resumed:
                self._dispatch(system_id)
            return {**result, "resumed": resumed}

    async def lifecycle(self, owner, system_id, action, command_id, revision):
        async with self.lock:
            self.ready()
            blocked = None
            if action in ("start", "resume"):
                # Ownership is checked before anything else is revealed, so a
                # setup problem cannot confirm that another owner's system exists.
                self.store.get_system(system_id, owner=owner)
                problems = self.agent_blockers(system_id)
                if problems:
                    blocked = {"code": "setup_blocked", "detail": " ".join(problems)}
            result = self.store.owner_lifecycle(owner, system_id, action, command_id, revision, blocked=blocked)
            if result["status"] == "accepted":
                if action == "stop":
                    await self.runtime.stop(system_id)
                elif action == "pause":
                    await self.runtime.pause(system_id, "manual_pause")
                else:
                    if action == "resume":
                        self.store.resume(system_id)
                    # A stopped company can be started again: stop leaves it
                    # stopped, and opening a run needs idle, so Start used to
                    # fail with a conflict and leave no way back.
                    self.store.reopen_for_start(system_id)
                    self._seed_run(system_id)
                    # Work runs in a backend-owned task: closing the tab, or
                    # this request returning, must not end the company's run.
                    self._dispatch(system_id)
                result = self.store.complete_lifecycle(system_id, command_id)
            return result

    async def reconcile(self, owner, system_id, attempt_id, evidence):
        """Close out a worker whose stop could never be confirmed.

        A mid-turn pause always lands here, because the Claude CLI does not
        acknowledge an interrupt - measured, not assumed. The store already
        demands a confirmed stop, written evidence and an explicit safe
        continuation point; the owner is the only one who can supply the first
        two, since they can see whether anything is still running. Unresolved
        tool actions are recorded as unknown rather than given an invented
        outcome: reconciliation is an admission of uncertainty, not a repair
        of it.
        """
        evidence = (evidence or "").strip()
        if not evidence:
            raise ValueError("Describe what you checked before reconciling")
        async with self.lock:
            self.ready()
            self.store.get_system(system_id, owner=owner)
            attempt = self.store.get_attempt(attempt_id)
            if attempt["system_id"] != system_id:
                raise NotFound("Attempt not found")
            if attempt["state"] != "unknown":
                raise Conflict("That worker does not need reconciliation")
            open_actions = [row[0] for row in self.store.db.execute(
                "SELECT action_id FROM actions WHERE attempt_id=? AND result IS NULL", (attempt_id,))]
            self.store.reconcile(
                attempt_id,
                evidence=f"{owner}: {evidence[:4000]}",
                stopped=True,
                retry_checkpoint={"reconciled_by": owner, "note": evidence[:2000],
                                  "at": self.store.clock()},
                action_results={action: {"outcome": "unknown", "reconciled": True} for action in open_actions})
            return {"attempt_id": attempt_id, "task_id": attempt["task_id"], "status": "reconciled"}

    async def checkpoint(self, owner, system_id, command_id):
        async with self.lock:
            self.ready()
            snapshot = self.store.save_checkpoint(system_id, owner=owner, command_id=command_id)
            return {"id": snapshot["id"], "cursor": snapshot["event_cursor"], "created_at": snapshot["saved_at"]}

    def download(self, owner, system_id, checkpoint_id, format):
        self.ready()
        return download_handoff(self.store.owner_checkpoint(owner, system_id, checkpoint_id), format)

    async def close(self):
        try:
            cycles = list(self.cycles.values())
            for cycle in cycles:
                cycle.cancel()
            if cycles:
                await asyncio.gather(*cycles, return_exceptions=True)
            if self.runtime and self.runtime.generation is not None:
                await self.runtime.close()
        finally:
            if self.store:
                self.store.close()
            self.store = None


async def startup(app):
    from core.constants import DATA_DIR
    service = SwarmService(Path(DATA_DIR) / "swarm")
    app.state.swarm = service
    await service.start()


async def shutdown(app):
    service = getattr(app.state, "swarm", None)
    if service:
        await service.close()
