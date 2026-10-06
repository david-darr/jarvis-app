"""Durable work, roadmap phase 4 (core/task_scheduler.py, services/
task_service.py, core/outbox.py; spec: the vault note "Durable Work - Phase 4
(Build Spec)").

What these prove: a scheduled run happens at most once, even across a crash;
one task never runs twice at once; only the schedule moves the schedule; a
long card holds up nothing; a run can be stopped and still counts what it
used; a channel message is retried, never sent twice by a restart, and
given up visibly.

Runs against a throwaway data folder; models and channels are stand-ins, so
nothing is spent or sent. Run: python scripts/test_durable_work.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_DATA = tempfile.TemporaryDirectory(prefix="jarvis-durable-test-", ignore_cleanup_errors=True)
os.environ["JARVIS_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import events, outbox, runs, session_manager_store as store, task_scheduler  # noqa: E402
from core.middleware import require_user  # noqa: E402
from routes import task_routes  # noqa: E402
from services import task_service as task_module  # noqa: E402
from services.agent_service import agent_service  # noqa: E402
from services.task_service import task_service  # noqa: E402

app = FastAPI()
app.include_router(task_routes.router)
app.dependency_overrides[require_user] = lambda: "david"


async def started(count: int = 1) -> None:
    """Until the stand-in model has begun `count` runs."""
    for _ in range(500):
        if SlowBrain.started >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the run never reached the model")


def ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class SlowBrain:
    """A stand-in model: reports some usage, then waits for `gate` (when
    set) before replying. Counts its runs and how many overlap."""
    started = 0
    active = 0
    most_at_once = 0
    gate: "asyncio.Event | None" = None
    reply = "done"

    async def connect(self): pass
    async def disconnect(self): pass

    async def events(self, prompt, stream=True):
        cls = SlowBrain
        cls.started += 1
        cls.active += 1
        cls.most_at_once = max(cls.most_at_once, cls.active)
        try:
            yield runs.usage_event({"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
            if cls.gate is not None:
                await cls.gate.wait()
            else:
                await asyncio.sleep(0.01)
            yield runs.text(cls.reply)
            yield runs.result(True)
        finally:
            cls.active -= 1


class Base(unittest.TestCase):
    def setUp(self):
        SlowBrain.started = SlowBrain.active = SlowBrain.most_at_once = 0
        SlowBrain.gate = None
        SlowBrain.reply = "done"
        self.created = []
        self.sent = []
        self.send_ok = True

        async def send(channel, text):
            self.sent.append((channel, text))
            return self.send_ok if not callable(self.send_ok) else self.send_ok()

        for target, attribute, value in (
                (task_scheduler, "_task_brain", lambda task: SlowBrain()),
                (task_scheduler, "task_endpoint_id", lambda task: "ep-test"),
                ("core.channels.registry", "send_to_channel", send)):
            p = patch(f"{target}.{attribute}", value) if isinstance(target, str) else patch.object(target, attribute, value)
            p.start()
            self.addCleanup(p.stop)
        self.usage = []
        p = patch("core.token_usage.record_usage", side_effect=lambda ep, usage: self.usage.append((ep, usage)))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._clean)

    def _clean(self):
        for task_id in self.created:
            task_service.delete_task(task_id)
        with store.transaction() as conn:
            conn.execute("DELETE FROM deliveries")
        task_scheduler._lanes.update(schedule=None, card=None)

    def task(self, name, kind="interval", due_seconds_ago=1, **extra):
        fields = {"interval_seconds": 3600} if kind == "interval" else \
            {"run_at": ago(-3600)} if kind == "once" else {"run_time": "06:00"}
        task = task_service.create_task(name, f"do {name}", kind, **fields, **extra)
        self.created.append(task["id"])
        if due_seconds_ago is not None:
            task["next_run_at"] = ago(due_seconds_ago)
            task_service._save_tasks()
        return task

    def card(self, name, **extra):
        card = task_service.create_task(name, f"do {name}", "card", status="ready", **extra)
        self.created.append(card["id"])
        return card


class AtMostOnceTests(Base):
    def test_a_run_cut_off_by_a_crash_is_recorded_lost_and_not_run_again(self):
        task = self.task("brief", deliver_to_channel="discord")
        claimed = task_service.claim_occurrence(task["id"])  # the run starts, then JARVIS dies
        self.assertGreater(claimed["next_run_at"], ago(0), "the schedule moved on before any work")
        restarted = task_module.TaskService()  # the next start reads what was saved
        lost = restarted.recover_interrupted_runs()
        self.assertEqual([t["id"] for t in lost], [task["id"]])
        self.assertEqual(restarted.list_runs(task["id"])[0]["outcome"], "lost")
        self.assertEqual(restarted.due_tasks(), [], "the cut-off occurrence is not run again")
        self.assertEqual(restarted.recover_interrupted_runs(), [], "recorded once")
        with patch.object(task_scheduler, "task_service", restarted), patch.object(events, "emit") as emitted:
            task_scheduler._report_lost(lost[0])
        self.assertIn("cut off when JARVIS closed", emitted.call_args.args[1])
        row = store.connection().execute("SELECT channel, text FROM deliveries").fetchone()
        self.assertEqual(row["channel"], "discord")
        self.assertIn("Run now", row["text"])

    def test_a_cut_off_run_from_before_claims_moves_its_schedule_on(self):
        task = self.task("old build run")
        stored = task_service.get_task(task["id"])
        stored["run_started_at"] = time.time() - 60  # stamped by a build without run_source
        task_service._save_tasks()
        restarted = task_module.TaskService()
        restarted.recover_interrupted_runs()
        self.assertEqual(restarted.due_tasks(), [], "it would otherwise run the same occurrence again")

    def test_a_duplicate_tick_runs_a_due_task_once(self):
        task = self.task("tick twice")

        async def go():
            await asyncio.gather(task_scheduler._run_due(), task_scheduler._run_due())

        asyncio.run(go())
        self.assertEqual(SlowBrain.started, 1)
        self.assertEqual(len(task_service.list_runs(task["id"])), 1)
        stored = task_service.get_task(task["id"])
        self.assertLess(stored["next_run_at"], ago(-3700), "moved on one interval, not two")


class OneRunAtATimeTests(Base):
    def test_a_second_run_now_is_refused_while_the_first_runs(self):
        task = self.task("busy", due_seconds_ago=None)

        async def go():
            SlowBrain.gate = asyncio.Event()
            first = asyncio.create_task(task_scheduler.start_run(task, "manual"))
            await started()
            second = await task_scheduler.start_run(task, "manual")
            SlowBrain.gate.set()
            return await first, second

        self.assertEqual(asyncio.run(go()), (True, False))
        self.assertEqual(SlowBrain.started, 1)

    def test_a_trigger_during_a_run_waits_then_runs_once(self):
        task = self.task("triggered", due_seconds_ago=None)

        async def go():
            SlowBrain.gate = asyncio.Event()
            first = asyncio.create_task(task_scheduler.start_run(task, "manual"))
            await started()
            second = asyncio.create_task(task_scheduler.start_run({**task, "prompt": "with the event"}, "trigger",
                                                                  wait=True))
            await asyncio.sleep(0.05)
            self.assertEqual(SlowBrain.started, 1, "the trigger's run waits its turn")
            SlowBrain.gate.set()
            await asyncio.gather(first, second)

        asyncio.run(go())
        self.assertEqual((SlowBrain.started, SlowBrain.most_at_once), (2, 1))
        self.assertEqual([r["source"] for r in task_service.list_runs(task["id"])], ["trigger", "manual"])

    def test_the_scheduler_skips_a_task_that_is_already_running(self):
        task = self.task("due while running")

        async def go():
            SlowBrain.gate = asyncio.Event()
            manual = asyncio.create_task(task_scheduler.start_run(task, "manual"))
            await started()
            await task_scheduler._run_due()
            SlowBrain.gate.set()
            await manual

        asyncio.run(go())
        self.assertEqual(SlowBrain.started, 1)
        self.assertGreater(task_service.get_task(task["id"])["next_run_at"], ago(0), "this occurrence is covered")


class ScheduleTests(Base):
    def test_run_now_leaves_the_schedule_alone(self):
        once = self.task("one-shot", kind="once", due_seconds_ago=None)
        interval = self.task("hourly", due_seconds_ago=-1800)
        before = {t["id"]: (t["enabled"], t["next_run_at"]) for t in (once, interval)}
        for task in (once, interval):
            asyncio.run(task_scheduler.start_run(task, "manual"))
        after = {t: (task_service.get_task(t)["enabled"], task_service.get_task(t)["next_run_at"]) for t in before}
        self.assertEqual(after, before, "a one-shot still runs at its time; an interval keeps its clock")

    def test_a_late_scheduled_run_says_so(self):
        late = self.task("missed while off", due_seconds_ago=3 * 3600)
        on_time = self.task("on time", due_seconds_ago=5)
        asyncio.run(task_scheduler._run_due())
        late_run = task_service.list_runs(late["id"])[0]
        self.assertAlmostEqual(late_run["late_seconds"], 3 * 3600, delta=30)
        self.assertEqual(late_run["source"], "schedule")
        self.assertIsNone(task_service.list_runs(on_time["id"])[0]["late_seconds"])
        self.assertEqual(SlowBrain.started, 2, "each missed task catches up once")


class LaneTests(Base):
    def test_a_long_card_does_not_hold_up_a_due_task(self):
        card = self.card("long card")
        task = self.task("six o'clock brief")

        async def go():
            gates = {}

            def brain_for(t):
                brain = SlowBrain()
                if t["id"] == card["id"]:
                    gates["card"] = asyncio.Event()

                    async def events(prompt, stream=True):
                        yield runs.usage_event({"input_tokens": 1, "output_tokens": 1})
                        await gates["card"].wait()
                        yield runs.text("card result")
                        yield runs.result(True)
                    brain.events = events
                return brain

            with patch.object(task_scheduler, "_task_brain", brain_for):
                await task_scheduler._tick()
                for _ in range(100):
                    if task_service.list_runs(task["id"]):
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(task_service.list_runs(task["id"]), "the brief ran while the card was still running")
                self.assertEqual(task_service.get_task(card["id"])["status"], "running")
                gates["card"].set()
                await task_scheduler._lanes["card"]

        asyncio.run(go())
        self.assertEqual(task_service.get_task(card["id"])["status"], "review")

    def test_a_card_past_its_lease_is_not_taken_as_lost_while_it_still_runs(self):
        card = self.card("very long card")

        async def go():
            SlowBrain.gate = asyncio.Event()
            claimed = task_service.claim_next_card()
            run = asyncio.create_task(task_scheduler.start_run(claimed, "schedule"))
            await started()
            task_service.get_task(card["id"])["claimed_until"] = time.time() - 1  # 30 minutes have passed
            await task_scheduler.dispatch_cards()
            self.assertEqual(task_service.get_task(card["id"])["status"], "running")
            SlowBrain.gate.set()
            await run

        asyncio.run(go())
        self.assertEqual(SlowBrain.started, 1, "never started a second time beside itself")
        stored = task_service.get_task(card["id"])
        self.assertEqual((stored["status"], stored["attempts"]), ("review", 1))
        self.assertEqual([r["outcome"] for r in task_service.list_runs(card["id"])], ["succeeded"],
                         "no false 'lost' run in its history")


class StopTests(Base):
    def test_stopping_a_card_blocks_it_and_counts_what_it_used(self):
        card = self.card("stop me")

        async def go():
            SlowBrain.gate = asyncio.Event()
            claimed = task_service.claim_next_card()
            run = asyncio.create_task(task_scheduler.start_run(claimed, "schedule"))
            await started()
            await asyncio.sleep(0.02)  # past the usage report
            self.assertTrue(task_scheduler.stop_run(card["id"]))
            await run

        asyncio.run(go())
        stored = task_service.get_task(card["id"])
        self.assertEqual((stored["status"], stored["comments"][-1]["text"]), ("blocked", task_module.STOPPED_NOTE))
        self.assertEqual(task_service.list_runs(card["id"])[0]["outcome"], "stopped")
        self.assertEqual([u.total_tokens for _, u in self.usage], [120], "the stopped run's usage is counted")
        self.assertIsNone(task_service.claim_next_card(), "a stopped card is not retried by itself")

    def test_stopping_a_scheduled_task_records_it_and_clears_the_running_mark(self):
        task = self.task("stop the brief", due_seconds_ago=None)

        async def go():
            SlowBrain.gate = asyncio.Event()
            run = asyncio.create_task(task_scheduler.start_run(task, "manual"))
            await started()
            self.assertTrue(task_service.get_task(task["id"]).get("run_started_at"))
            task_scheduler.stop_run(task["id"])
            await run

        asyncio.run(go())
        self.assertEqual(task_service.list_runs(task["id"])[0]["outcome"], "stopped")
        self.assertNotIn("run_started_at", task_service.get_task(task["id"]))

    def test_the_stop_and_retry_routes(self):
        web = TestClient(app)
        task = self.task("idle", due_seconds_ago=None)
        self.assertEqual(web.post(f"/api/tasks/{task['id']}/stop").status_code, 409, "not running")
        self.assertEqual(web.post("/api/tasks/nope/stop").status_code, 404)
        delivered = outbox.enqueue("route:1", "discord", "hi")
        asyncio.run(outbox.send_due())
        self.assertEqual(web.post(f"/api/tasks/deliveries/{delivered}/retry").status_code, 409, "it landed")
        self.assertEqual(web.post("/api/tasks/deliveries/nope/retry").status_code, 404)


class OutboxTests(Base):
    def row(self, delivery_id):
        return outbox.get(delivery_id)

    def test_a_failed_send_is_retried_then_lands(self):
        answers = iter([False, False, True])
        self.send_ok = lambda: next(answers)
        delivery = outbox.enqueue("t:1", "discord", "hello")
        now = time.time()
        asyncio.run(outbox.send_due(now))
        self.assertEqual((self.row(delivery)["status"], self.row(delivery)["attempts"]), ("pending", 1))
        asyncio.run(outbox.send_due(now + 30))
        self.assertEqual(self.row(delivery)["attempts"], 1, "not before its retry time (1 minute)")
        asyncio.run(outbox.send_due(now + 90))  # retry times run from when a send finished
        asyncio.run(outbox.send_due(now + 90 + 5 * 60 + 30))
        landed = self.row(delivery)
        self.assertEqual((landed["status"], landed["attempts"], landed["text"]), ("delivered", 3, ""))
        self.assertEqual(len(self.sent), 3)

    def test_a_send_that_keeps_failing_is_given_up_after_a_day_and_said(self):
        self.send_ok = False
        delivery = outbox.enqueue("t:2", "discord", "hello", label="Brief")
        with store.transaction() as conn:
            conn.execute("UPDATE deliveries SET created_at = created_at - ? WHERE id = ?",
                         (outbox.GIVE_UP_SECONDS, delivery))
        with patch.object(events, "emit") as emitted:
            asyncio.run(outbox.send_due())
        self.assertEqual(self.row(delivery)["status"], "failed")
        self.assertEqual(emitted.call_args.args[0], "delivery.failed")
        self.assertIn("Brief", emitted.call_args.args[1])
        self.send_ok = True
        outbox.retry(delivery)
        asyncio.run(outbox.send_due())
        self.assertEqual(self.row(delivery)["status"], "delivered", "a person can send it again")

    def test_a_message_cut_off_mid_send_is_unknown_and_never_resent(self):
        delivery = outbox.enqueue("t:3", "discord", "hello")
        with store.transaction() as conn:
            conn.execute("UPDATE deliveries SET status = 'sending' WHERE id = ?", (delivery,))
        self.assertEqual(outbox.recover(), 1)
        asyncio.run(outbox.send_due(time.time() + 10 * 86400))
        self.assertEqual((self.row(delivery)["status"], self.sent), ("unknown", []))

    def test_the_same_message_queued_twice_is_sent_once(self):
        first = outbox.enqueue("t:4", "discord", "hello")
        self.assertEqual(outbox.enqueue("t:4", "discord", "hello"), first)
        asyncio.run(outbox.send_due())
        self.assertEqual(len(self.sent), 1)

    def test_only_the_newest_finished_messages_are_kept(self):
        with patch.object(outbox, "KEEP_FINISHED", 2):
            ids = [outbox.enqueue(f"t:keep{i}", "discord", "x") for i in range(4)]
            for _ in ids:
                asyncio.run(outbox.send_due())
                time.sleep(0.01)
        kept = [d for d in ids if self.row(d)]
        self.assertEqual(len(kept), 2)

    def test_a_task_run_carries_its_delivery(self):
        task = self.task("delivered brief", due_seconds_ago=None, deliver_to_channel="discord")
        asyncio.run(task_scheduler.start_run(task, "manual"))
        asyncio.run(outbox.send_due())
        run = task_service.list_runs(task["id"])[0]
        self.assertEqual((run["delivery"]["status"], run["delivered"]), ("delivered", True))
        self.assertEqual(self.sent, [("discord", "**delivered brief**\ndone")])

    def test_agent_and_trigger_messages_go_through_the_outbox(self):
        agent = agent_service.create("Courier", deliver_to_channel="discord")
        self.addCleanup(agent_service.delete, agent["id"])
        item = agent_service.add_item(agent["id"], "report", "All quiet", "nothing new")
        agent_service.notify(item)
        from services.trigger_service import trigger_service
        trigger_service.notify({"id": "tr1", "name": "Stripe", "action": "card", "agent_id": None,
                                "deliver_to_channel": "discord", "prompt": "p"},
                               {"id": "pend1", "event_type": "charge", "summary": "a charge"})
        texts = [r["text"] for r in store.connection().execute("SELECT text FROM deliveries ORDER BY created_at")]
        self.assertEqual(len(texts), 2)
        self.assertIn("Courier", texts[0])
        self.assertIn("Stripe", texts[1])


class StoreTests(unittest.TestCase):
    def test_a_v4_store_is_copied_then_upgraded_with_the_outbox(self):
        folder = tempfile.mkdtemp(dir=_DATA.name)
        path = os.path.join(folder, "sessions.db")
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        old.execute("INSERT INTO meta VALUES ('schema_version', '4')")
        old.commit()
        old.close()
        store.close()
        try:
            with patch.object(store, "DB_FILE", path):
                conn = store.connection()
                self.assertTrue(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'deliveries'").fetchone())
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "5")
                self.assertTrue(os.path.exists(path + ".pre-v4"))
                store.close()
        finally:
            store.close()


def tearDownModule():
    store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
