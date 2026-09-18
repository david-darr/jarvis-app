"""Isolated Swarm engine checks. No provider, app, credential, or live data imports.

Run: .venv/Scripts/python.exe scripts/test_swarm.py
"""
import asyncio
import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.swarm.budget import BudgetLimit
from core.swarm.checkpoints import save_handoff
from core.swarm.models import Conflict, EventKind, PersistenceFault, WorkerEvent
from core.swarm.runtime import SwarmRuntime
from core.swarm.store import SwarmStore


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Fixture:
    def setup_store(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jarvis-swarm-test-")
        self.root = Path(self.temporary.name)
        self.clock = Clock()
        self.store = SwarmStore(self.root / "swarm.db", clock=self.clock)
        self.limit = BudgetLimit(10000)
        self.pool = self.store.create_pool("Fake account", self.limit)
        self.system, self.lead = self.store.create_system(
            "owner", "Example company", "Build an app", pool_id=self.pool,
            system_limit=self.limit, lead_limit=self.limit)
        self.run = self.store.create_run(self.system, "Implement the app", self.limit)
        self.agent = self.store.add_agent(self.system, "Developer", "backend", self.pool, self.limit)
        self.task = self.new_task(self.agent, "Implement endpoint")

    def new_task(self, agent, objective, **kwargs):
        return self.store.create_task(self.system, self.run, agent, objective,
                                      command_id="task:" + objective, **kwargs)

    def teardown_store(self):
        self.store.close()
        self.temporary.cleanup()

    def own(self):
        self.generation = self.store.acquire_runtime("test-runtime")

    def claim(self, task=None, maximum=10):
        return self.store.reserve(task or self.task, "test-runtime", self.generation, maximum)

    def begin(self, attempt):
        return self.store.begin(attempt, "test-runtime", self.generation)

    def complete(self, attempt, result=None):
        self.store.finish(attempt, "test-runtime", self.generation,
                          result or {"artifact": "candidate.diff"}, usage_complete=True)

    def event(self, attempt, event_id, kind, data):
        return self.store.worker_event(attempt, "test-runtime", self.generation, event_id, kind, data)


class StoreTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_store()

    def tearDown(self):
        self.teardown_store()

    def test_concurrent_claim_uses_real_separate_connections(self):
        self.own()
        barrier = threading.Barrier(2)

        def reserve():
            store = SwarmStore(self.store.path, clock=self.clock)
            try:
                barrier.wait()
                return store.reserve(self.task, "test-runtime", self.generation, 10)
            finally:
                store.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(reserve) for _ in range(2)]
            results = [future.result() for future in futures]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_concurrent_first_open_initializes_schema_once(self):
        barrier = threading.Barrier(2)

        def open_store():
            barrier.wait()
            store = SwarmStore(self.root / "new.db")
            try:
                return store.db.execute("PRAGMA user_version").fetchone()[0]
            finally:
                store.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(open_store) for _ in range(2)]
            self.assertEqual([future.result() for future in futures], [3, 3])

    def test_single_runtime_owner_and_expired_owner_fenced(self):
        self.own()
        with self.assertRaises(Conflict):
            self.store.acquire_runtime("second")
        attempt = self.claim()
        self.clock.now += 31
        self.store.acquire_runtime("second")
        with self.assertRaises(Conflict):
            self.begin(attempt)
        self.assertEqual(self.store.get_attempt(attempt)["state"], "cancelled")
        self.assertEqual(self.store.get_attempt(attempt)["held"], 0)

    def test_pause_wins_pending_launch(self):
        self.own()
        attempt = self.claim()
        self.store.pause(self.system)
        self.assertFalse(self.begin(attempt))
        self.assertEqual(self.store.get_attempt(attempt)["state"], "cancelled")
        self.assertIsNone(self.claim())

    def test_agent_threshold_pauses_entire_company(self):
        small = self.store.add_agent(self.system, "Small budget", "qa", self.pool, BudgetLimit(10))
        task = self.new_task(small, "Test endpoint")
        self.own()
        attempt = self.claim(task, 8)
        self.begin(attempt)
        self.store.record_usage(attempt, "provider-request", 8)
        self.assertEqual(self.store.get_system(self.system)["state"], "pausing")
        self.assertIsNone(self.claim())

    def test_shared_pool_reservation_is_not_double_spent(self):
        pool = self.store.create_pool("Shared limited account", BudgetLimit(100))
        a = self.store.add_agent(self.system, "A", "dev", pool, self.limit)
        b = self.store.add_agent(self.system, "B", "qa", pool, self.limit)
        first, second = self.new_task(a, "First"), self.new_task(b, "Second")
        self.own()
        self.assertIsNotNone(self.claim(first, 50))
        self.assertIsNone(self.claim(second, 50))
        held = self.store.db.execute("SELECT SUM(held) FROM attempts WHERE pool_id=?", (pool,)).fetchone()[0]
        self.assertEqual(held, 50)

    def test_pool_exhaustion_pauses_other_companies_even_when_agent_limit_also_hits(self):
        pool = self.store.create_pool("Shared", BudgetLimit(10))
        small = self.store.add_agent(self.system, "Limited", "qa", pool, BudgetLimit(10))
        task = self.new_task(small, "Limited task")
        other, lead = self.store.create_system("owner", "Other", "Other mission", pool_id=pool,
                                                system_limit=self.limit, lead_limit=self.limit)
        self.store.create_run(other, "Other goal", self.limit)
        self.own()
        attempt = self.claim(task, 8)
        self.begin(attempt)
        self.store.record_usage(attempt, "used", 8)
        for system in (self.system, other):
            self.assertEqual(self.store.get_system(system)["state"], "pausing")
        snapshot = self.store.save_checkpoint(other)
        pool_budget = next(budget for budget in snapshot["budgets"] if budget["scope"] == "pool")
        self.assertEqual(pool_budget["used"], 8)

    def test_crash_preserves_uncertain_action_and_stale_result_is_rejected(self):
        self.own()
        old_generation = self.generation
        attempt = self.claim()
        self.begin(attempt)
        self.event(attempt, "intent", "action_started", {"action_id": "write", "intent": {"path": "file.py"}})
        self.clock.now += 31
        self.generation = self.store.acquire_runtime("test-runtime")
        self.assertEqual(self.store.get_attempt(attempt)["state"], "unknown")
        self.assertEqual(self.store.get_task(self.task)["state"], "blocked")
        self.store.finalize_pause(self.system)
        with self.assertRaises(Conflict):
            self.store.resume(self.system)
        with self.assertRaises(Conflict):
            self.store.finish(attempt, "test-runtime", old_generation, {"stale": True})
        with self.assertRaises(Conflict):
            self.store.reconcile(attempt, evidence="Worker stopped", stopped=True,
                                 retry_checkpoint={"next": "Inspect existing output"})
        self.store.reconcile(attempt, evidence="Inspected file; write completed, process stopped", stopped=True,
                             action_results={"write": {"written": True}},
                             retry_checkpoint={"next": "Validate file; do not repeat write"})
        self.store.resume(self.system)
        replacement = self.claim()
        self.begin(replacement)
        with self.assertRaises(Conflict):
            self.event(attempt, "late-checkpoint", "checkpoint", {"next": "bad"})
        self.assertIn("do not repeat", self.store.get_task(self.task)["checkpoint"])
        self.assertEqual(self.store.get_attempt(replacement)["state"], "started")

    def test_idempotent_commands_events_and_usage(self):
        self.assertEqual(self.new_task(self.agent, "Implement endpoint"), self.task)
        message = self.store.send_message(self.system, self.lead, self.agent, "Question", command_id="m1")
        self.assertEqual(message, self.store.send_message(self.system, self.lead, self.agent, "Question", command_id="m1"))
        with self.assertRaises(Conflict):
            self.store.send_message(self.system, self.lead, self.agent, "Changed", command_id="m1")
        self.own()
        attempt = self.claim()
        self.begin(attempt)
        self.event(attempt, "partial", "checkpoint", {"next": "QA"})
        self.event(attempt, "partial", "checkpoint", {"next": "QA"})
        self.store.record_usage(attempt, "request-1", 3)
        self.store.record_usage(attempt, "request-1", 3)
        self.assertEqual(self.store.get_attempt(attempt)["used"], 3)
        self.assertEqual(self.store.get_attempt(attempt)["held"], 7)
        with self.assertRaises(Conflict):
            self.store.record_usage(attempt, "request-1", 4)
        self.assertEqual(sum(e["kind"] == "checkpoint" for e in self.store.events(self.system)), 1)

    def test_cyclic_and_cross_system_dependencies_rejected(self):
        other = self.new_task(self.lead, "Review endpoint", dependencies=[self.task])
        with self.assertRaises(Conflict):
            self.store.add_dependency(self.task, other)
        with self.assertRaises(Conflict):
            self.store.add_dependency(self.task, self.task)
        system, lead = self.store.create_system("owner", "Other", "Different app", pool_id=self.pool,
                                                 system_limit=self.limit, lead_limit=self.limit)
        run = self.store.create_run(system, "Other goal", self.limit)
        foreign = self.store.create_task(system, run, lead, "Other task", command_id="foreign")
        with self.assertRaises(Conflict):
            self.store.add_dependency(self.task, foreign)
        with self.assertRaises(Conflict):
            self.store.send_message(self.system, self.lead, lead, "Leak", command_id="leak")

    def test_dependency_waits_for_reviewed_result(self):
        dependent = self.new_task(self.lead, "QA endpoint", dependencies=[self.task])
        self.own()
        attempt = self.claim()
        self.begin(attempt)
        self.complete(attempt)
        self.assertIsNone(self.claim(dependent))
        task = self.store.get_task(self.task)
        with self.assertRaises(Conflict):
            self.store.accept_result(self.task, expected_revision=0, evidence="wrong revision")
        self.store.accept_result(self.task, expected_revision=task["revision"], evidence="Tests passed")
        self.assertIsNotNone(self.claim(dependent))

    def test_a_warning_below_the_owners_threshold_does_not_stop_the_company(self):
        """From the first live run: the real Claude CLI reports
        `allowed_warning` on its seven-day window at 50% utilization. A flag
        without a number is not a reason to halt; the configured threshold is."""
        self.store.update_quota(self.pool, "seven_day", used_percent=50, status="warning",
                                observed_at=self.clock(), valid_until=self.clock() + 900)
        self.assertEqual(self.store.get_system(self.system)["state"], "active")
        self.own()
        self.assertIsNotNone(self.claim(), "a warned account with real headroom still works")

    def test_a_warning_at_the_threshold_or_without_a_number_stops_everything(self):
        self.store.update_quota(self.pool, "seven_day", used_percent=80, status="warning",
                                observed_at=self.clock(), valid_until=self.clock() + 900)
        self.assertEqual(self.store.get_system(self.system)["state"], "pausing")
        self.store.update_quota(self.pool, "seven_day", used_percent=None, status="warning",
                                observed_at=self.clock(), valid_until=self.clock() + 900)
        self.assertIn("quota:seven_day", self.store.get_system(self.system)["reason"])

    def test_a_rejection_stops_the_company_at_any_utilization(self):
        self.store.update_quota(self.pool, "five_hour", used_percent=1, status="rejected",
                                observed_at=self.clock(), valid_until=self.clock() + 900)
        self.assertEqual(self.store.get_system(self.system)["state"], "pausing")

    def test_exhausted_quota_handoff_and_reopen_preserve_context(self):
        message = self.store.send_message(self.system, None, self.lead, "Build the idea", command_id="idea")
        self.store.update_quota(self.pool, "weekly", used_percent=99, status="allowed",
                                observed_at=self.clock(), valid_until=self.clock() + 60)
        self.store.finalize_pause(self.system)
        handoff = save_handoff(self.store, self.system, self.root / "handoffs")
        snapshot = handoff["snapshot"]
        self.assertIn("Build the idea", handoff["markdown"].read_text(encoding="utf-8"))
        self.assertEqual(json.loads(handoff["json"].read_text(encoding="utf-8")), snapshot)
        self.store.close()
        self.store = SwarmStore(self.root / "swarm.db", clock=self.clock)
        self.assertEqual(self.store.latest_checkpoint(self.system), snapshot)
        self.assertEqual(snapshot["messages"][0]["id"], message)
        self.assertEqual(self.store.events(self.system, snapshot["event_cursor"]), [])
        self.assertEqual(self.store.get_system(self.system)["state"], "paused")
        with self.assertRaises(Conflict):
            self.store.resume(self.system)

    def test_quota_expiry_on_idle_specialist_pauses_everyone(self):
        separate = self.store.create_pool("Specialist", self.limit)
        self.store.add_agent(self.system, "Research", "research", separate, self.limit)
        self.store.update_quota(separate, "daily", used_percent=10, status="allowed",
                                observed_at=self.clock(), valid_until=self.clock() + 1)
        self.own()
        self.clock.now += 2
        self.assertIsNone(self.claim())
        self.assertEqual(self.store.get_system(self.system)["state"], "pausing")

    def test_final_usage_settlement_releases_hold_only_after_recorded_stop(self):
        self.own()
        attempt = self.claim()
        self.begin(attempt)
        self.store.record_usage(attempt, "request", 3)
        with self.assertRaises(Conflict):
            self.store.settle_usage(attempt, total_units=3, evidence="Premature")
        self.store.interrupt(attempt, "test-runtime", self.generation, stopped=True, reason="manual")
        self.assertEqual(self.store.get_attempt(attempt)["held"], 7)
        with self.assertRaises(Conflict):
            self.store.settle_usage(attempt, total_units=4, evidence="Missing request event")
        self.store.settle_usage(attempt, total_units=3, evidence="Final provider report")
        self.store.settle_usage(attempt, total_units=3, evidence="Final provider report")
        self.store.record_usage(attempt, "request", 3)
        self.assertEqual(self.store.get_attempt(attempt)["used"], 3)
        self.assertEqual(self.store.get_attempt(attempt)["held"], 0)
        self.assertEqual(sum(e["kind"] == "usage.settled" for e in self.store.events(self.system)), 1)

    def test_unknown_usage_retains_reservation_and_overrun_pauses(self):
        self.own()
        attempt = self.claim()
        self.begin(attempt)
        self.store.record_usage(attempt, "partial", 2)
        self.store.finish(attempt, "test-runtime", self.generation, {}, usage_complete=False)
        self.assertEqual(self.store.get_attempt(attempt)["held"], 8)
        self.store.record_usage(attempt, "late", 9)
        self.assertIn("worker_exceeded", self.store.get_system(self.system)["reason"])

    def test_export_failure_leaves_database_checkpoint_and_raises(self):
        with patch("core.swarm.checkpoints._atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                save_handoff(self.store, self.system, self.root / "handoffs")
        self.assertIsNotNone(self.store.latest_checkpoint(self.system))

    def test_sqlite_write_failure_latches_admission_fault(self):
        self.own()
        self.store.db.execute("PRAGMA query_only=ON")
        with self.assertRaises(PersistenceFault):
            self.claim()
        self.store.db.execute("PRAGMA query_only=OFF")
        with self.assertRaises(PersistenceFault):
            self.claim()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

    def test_budget_validation_and_checkpoint_reserve(self):
        for invalid in (True, -1, 0, 1.5):
            with self.assertRaises(ValueError):
                BudgetLimit(invalid)
        limit = BudgetLimit(100, checkpoint_reserve=10)
        self.assertTrue(limit.admits(20, 30, 20))
        self.assertFalse(limit.admits(20, 30, 21))
        self.assertTrue(limit.exhausted(70))


class FakeWorker:
    def __init__(self, events=(), *, block=False, confirms_stop=True):
        self.script = events
        self.block = block
        self.confirms_stop = confirms_stop
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_calls = 0
        self.assignment = None

    async def events(self, assignment):
        self.assignment = assignment
        self.started.set()
        for event in self.script:
            yield event
        if self.block:
            await self.release.wait()

    async def cancel(self):
        self.cancel_calls += 1
        self.release.set()
        return self.confirms_stop


class RuntimeTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_store()
        self.runtime = SwarmRuntime(self.store, self.root / "handoffs", poll_interval=0.01, stop_timeout=0.1)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.teardown_store()

    async def wait_for(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), timeout=2)

    async def test_fake_worker_records_output_actions_usage_and_review(self):
        worker = FakeWorker([
            WorkerEvent(EventKind.TEXT, "text", {"text": "Implementing"}),
            WorkerEvent(EventKind.CHECKPOINT, "checkpoint", {"next": "Test", "artifact": "candidate.diff"}),
            WorkerEvent(EventKind.ACTION_STARTED, "intent", {"action_id": "write", "intent": {"path": "candidate.diff"}}),
            WorkerEvent(EventKind.ACTION_FINISHED, "outcome", {"action_id": "write", "result": {"written": True}}),
            WorkerEvent(EventKind.USAGE, "request", {"units": 3}),
            WorkerEvent(EventKind.RESULT, "result", {"result": {"artifact": "candidate.diff"}, "usage_complete": True}),
        ])
        attempt = await self.runtime.run_task(self.task, worker, max_units=10)
        self.assertEqual(self.store.get_attempt(attempt)["used"], 3)
        self.assertEqual(self.store.get_attempt(attempt)["held"], 0)
        self.assertEqual(self.store.get_task(self.task)["state"], "review")
        self.assertEqual(worker.assignment.agent_id, self.agent)
        self.assertIsNone(self.runtime.fault)

    async def test_quota_pause_cancels_entire_running_company(self):
        other = self.new_task(self.lead, "Coordinate")
        workers = [FakeWorker(block=True), FakeWorker(block=True)]
        runners = [asyncio.create_task(self.runtime.run_task(task, worker, max_units=10))
                   for task, worker in zip((self.task, other), workers)]
        await asyncio.wait_for(asyncio.gather(*(worker.started.wait() for worker in workers)), 2)
        self.store.update_quota(self.pool, "weekly", used_percent=81, status="allowed",
                                observed_at=self.clock(), valid_until=self.clock() + 60)
        attempts = await asyncio.wait_for(asyncio.gather(*runners), 2)
        self.assertTrue(all(worker.cancel_calls for worker in workers))
        self.assertTrue(all(self.store.get_attempt(attempt)["state"] == "cancelled" for attempt in attempts))
        self.assertEqual(self.store.get_system(self.system)["state"], "paused")
        self.assertIn(self.system, self.runtime.handoffs)

    async def test_agent_budget_event_cancels_other_running_workers(self):
        small = self.store.add_agent(self.system, "Limited", "qa", self.pool, BudgetLimit(10))
        task = self.new_task(small, "Bounded test")
        other = FakeWorker(block=True)
        other_runner = asyncio.create_task(self.runtime.run_task(self.task, other, max_units=10))
        await other.started.wait()
        worker = FakeWorker([WorkerEvent(EventKind.USAGE, "quota", {"units": 8})], block=True)
        await self.runtime.run_task(task, worker, max_units=8)
        await asyncio.wait_for(other_runner, 2)
        self.assertEqual(self.store.get_system(self.system)["state"], "paused")
        self.assertTrue(other.cancel_calls)

    async def test_cancellation_timeout_leaves_unknown_attempt(self):
        class HangingCancel(FakeWorker):
            async def cancel(self):
                await asyncio.Event().wait()

        worker = HangingCancel(block=True)
        runner = asyncio.create_task(self.runtime.run_task(self.task, worker, max_units=10))
        await worker.started.wait()
        await asyncio.wait_for(self.runtime.pause(self.system), 2)
        attempt = await runner
        self.assertEqual(self.store.get_attempt(attempt)["state"], "unknown")
        self.assertEqual(self.store.get_system(self.system)["state"], "paused")

    async def test_hanging_finalizer_cannot_be_recorded_as_success(self):
        class HangingFinalizer(FakeWorker):
            async def events(self, assignment):
                try:
                    yield WorkerEvent(EventKind.RESULT, "result", {"result": {"candidate": True}})
                finally:
                    await asyncio.Event().wait()

        attempt = await asyncio.wait_for(self.runtime.run_task(self.task, HangingFinalizer(), max_units=10), 2)
        self.assertEqual(self.store.get_attempt(attempt)["state"], "unknown")
        self.assertEqual(self.store.get_task(self.task)["state"], "blocked")
        with self.assertRaises(Conflict):
            self.store.resume(self.system)

    async def test_worker_failure_saves_partial_checkpoint(self):
        class BrokenWorker(FakeWorker):
            async def events(self, assignment):
                yield WorkerEvent(EventKind.CHECKPOINT, "saved", {"next": "Resume validation"})
                raise RuntimeError("Simulated provider failure")

        with self.assertRaisesRegex(RuntimeError, "Simulated"):
            await self.runtime.run_task(self.task, BrokenWorker(), max_units=10)
        self.assertEqual(self.store.get_system(self.system)["state"], "paused")
        self.assertIn("Resume validation", self.runtime.handoffs[self.system]["markdown"].read_text(encoding="utf-8"))

    async def test_paused_at_launch_never_calls_worker(self):
        worker = FakeWorker(block=True)
        runner = asyncio.create_task(self.runtime.run_task(self.task, worker, max_units=10))
        await asyncio.sleep(0)
        handoff = await self.runtime.pause(self.system)
        await runner
        self.assertFalse(worker.started.is_set())
        self.assertEqual(handoff["snapshot"]["system"]["state"], "paused")

    async def test_exhausted_account_makes_handoff_without_starting_worker(self):
        self.store.update_quota(self.pool, "weekly", used_percent=100, status="rejected",
                                observed_at=self.clock(), valid_until=self.clock() + 60)
        worker = FakeWorker(block=True)
        self.assertIsNone(await self.runtime.run_task(self.task, worker, max_units=10))
        self.assertFalse(worker.started.is_set())
        self.assertIn(self.system, self.runtime.handoffs)

    async def test_unconfirmed_native_stop_blocks_resume(self):
        worker = FakeWorker([WorkerEvent(EventKind.CHECKPOINT, "checkpoint", {"next": "Inspect native child"})],
                            block=True, confirms_stop=False)
        runner = asyncio.create_task(self.runtime.run_task(self.task, worker, max_units=10))
        await worker.started.wait()
        await self.wait_for(lambda: "Inspect" in self.store.get_task(self.task)["checkpoint"])
        handoff = await self.runtime.pause(self.system)
        attempt = await runner
        self.assertEqual(self.store.get_attempt(attempt)["state"], "unknown")
        self.assertEqual(self.store.get_attempt(attempt)["stopped"], 0)
        self.assertIn('"state": "unknown"', handoff["markdown"].read_text(encoding="utf-8"))
        with self.assertRaises(Conflict):
            self.store.resume(self.system)

    async def test_persistence_failure_stops_active_worker_and_new_dispatch(self):
        worker = FakeWorker(block=True)
        runner = asyncio.create_task(self.runtime.run_task(self.task, worker, max_units=10))
        await worker.started.wait()
        self.store.db.execute("PRAGMA query_only=ON")
        result = await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 2)
        self.assertIsInstance(result[0], PersistenceFault)
        self.assertTrue(worker.cancel_calls)
        self.assertIsNotNone(self.runtime.fault)
        self.assertNotIn(self.system, self.runtime.handoffs)
        with self.assertRaises(PersistenceFault):
            await self.runtime.run_task(self.task, FakeWorker(), max_units=10)
        # Failed persistence leaves the durable started marker for restart reconciliation.
        self.assertEqual(self.store.get_task(self.task)["state"], "running")

    async def test_restart_runtime_exports_recovery_handoff_without_replay(self):
        attempt = self.store.reserve(self.task, self.runtime.runtime_id, self.runtime.generation, 10)
        self.store.begin(attempt, self.runtime.runtime_id, self.runtime.generation)
        await self.runtime.close()
        self.runtime = SwarmRuntime(self.store, self.root / "handoffs", poll_interval=0.01)
        await self.runtime.start()
        self.assertEqual(self.store.get_attempt(attempt)["state"], "unknown")
        self.assertEqual(self.store.get_system(self.system)["state"], "paused")
        self.assertIn(self.system, self.runtime.handoffs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
