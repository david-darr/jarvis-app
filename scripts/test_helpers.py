"""Helpers, roadmap phase 5 (core/helpers.py; spec: the vault note "Helpers -
Phase 5 (Build Spec)").

What these prove: up to 3 read-only helpers run in parallel and report back;
one failing does not sink the others; each is stopped at its time and token
limits keeping what it had; they stop with their parent; no more than 3 run
at once; a helper can reach only the read tools and the browser, whatever it
names; a restart marks the unfinished ones lost and never reruns them; and a
chat can collect its own results later, never another chat's.

Runs against a throwaway data folder; the helper model is a stand-in, so
nothing is spent. Run: python scripts/test_helpers.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_DATA = tempfile.TemporaryDirectory(prefix="jarvis-helpers-test-", ignore_cleanup_errors=True)
os.environ["JARVIS_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import (helpers, model_endpoints, runs, session_manager_store as store, settings as settings_store,  # noqa: E402
                  system_prompt, tool_registry)
from core.external_brain import ExternalBrain  # noqa: E402
from core.middleware import require_admin  # noqa: E402
from core.turn_taint import TurnTaint  # noqa: E402
from routes import settings_routes  # noqa: E402

ENDPOINT = {"id": "ep-local", "name": "Local", "kind": "local", "model": "stand-in"}


class FakeBrain:
    """A stand-in helper model. Its reply depends on the goal: "fail" raises,
    "slow" writes a little then waits for `gate`, "huge" reports more tokens
    than a helper may spend, anything else answers at once."""
    started = 0
    active = 0
    most_at_once = 0
    gate: "asyncio.Event | None" = None

    def __init__(self):
        self.model = "stand-in"
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True

    async def events(self, prompt, stream=True):
        cls = FakeBrain
        cls.started += 1
        cls.active += 1
        cls.most_at_once = max(cls.most_at_once, cls.active)
        try:
            yield runs.usage_event({"input_tokens": 50_000 if "huge" in prompt else 300, "output_tokens": 20})
            if "fail" in prompt:
                raise RuntimeError("model down")
            if "slow" in prompt or "huge" in prompt:
                yield runs.text("partial findings")
                await (cls.gate.wait() if cls.gate else asyncio.Event().wait())
            yield runs.text(f"Found it for: {prompt.splitlines()[0]}")
            yield runs.result(True)
        finally:
            cls.active -= 1


async def until_statuses(test, expected) -> None:
    """Wait, inside the loop, for the helpers to reach these statuses. Checked
    before the loop ends: asyncio.run() cancels whatever is still running when
    it closes, which would make any helper look stopped."""
    for _ in range(200):
        if test.statuses() == expected:
            return
        await asyncio.sleep(0.01)
    test.assertEqual(test.statuses(), expected)


class Ctx:
    """What a tool call carries, for a chat or a task."""
    def __init__(self, session_id="chat-1", agent_id=None):
        self.session_id, self.agent_id, self.turn_taint = session_id, agent_id, TurnTaint()


class Base(unittest.TestCase):
    def setUp(self):
        FakeBrain.started = FakeBrain.active = FakeBrain.most_at_once = 0
        FakeBrain.gate = None
        self.usage = []
        for target, attribute, value in ((helpers, "_brain", lambda endpoint: FakeBrain()),
                                         (helpers, "helper_endpoint", lambda: ENDPOINT),
                                         (helpers, "CHECK_SECONDS", 0.02)):
            p = patch.object(target, attribute, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch("core.token_usage.record_usage", side_effect=lambda ep, usage: self.usage.append((ep, usage.total_tokens)))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._clean)

    def _clean(self):
        with store.transaction() as conn:
            conn.execute("DELETE FROM helpers")
            conn.execute("DELETE FROM runs")
        helpers._active.clear()

    def delegate(self, goals, ctx=None):
        return asyncio.run(helpers.delegate([{"goal": g} for g in goals], ctx or Ctx()))

    def statuses(self):
        with store.transaction() as conn:
            return [r["status"] for r in conn.execute("SELECT status FROM helpers ORDER BY created_at")]


class FanOutTests(Base):
    def test_three_helpers_run_in_parallel_and_report_back(self):
        ctx = Ctx()

        async def go():
            token = runs.CURRENT.set(runs.RunContext("chat", run_id="parent-run", session_id="chat-1"))
            try:
                return await helpers.delegate([{"goal": "find A"}, {"goal": "find B", "context": "B is a city"},
                                               {"goal": "find C"}], ctx)
            finally:
                runs.CURRENT.reset(token)

        report = asyncio.run(go())
        self.assertEqual(self.statuses(), ["done", "done", "done"])
        for goal in ("find A", "find B", "find C"):
            self.assertIn(f"Found it for: {goal}", report)
        self.assertEqual(report.count("UNTRUSTED-"), 6, "each finding fenced as untrusted data")
        self.assertTrue(ctx.turn_taint.tainted, "reading helper findings taints the turn")
        self.assertIn("helper results", ctx.turn_taint.sources)
        self.assertEqual([t for _, t in self.usage], [320, 320, 320], "each helper's tokens count on Home")
        lineage = [r for r in store.list_runs(limit=10) if r["surface"] == "helper"]
        self.assertEqual({r["parent_id"] for r in lineage}, {"parent-run"})
        self.assertEqual(len(lineage), 3)

    def test_a_chat_whose_tools_run_in_their_own_task_still_names_its_run(self):
        """Claude Code calls JARVIS's tools from a task of its own, where the
        current run is not set (found live 2026-10-06): the chat's run is
        found by its id."""
        chat_run = runs.RunContext("chat", run_id="claude-turn", session_id="chat-1")

        async def go():
            runs.enter(chat_run)
            try:
                return await asyncio.get_running_loop().create_task(self._in_fresh_context())
            finally:
                runs.leave(chat_run)

        asyncio.run(go())
        with store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT parent_run_id FROM helpers").fetchone()[0], "claude-turn")
        self.assertIsNone(runs.current_for("chat-1"), "forgotten once the turn ends")

    async def _in_fresh_context(self):
        runs.CURRENT.set(None)  # as in a task the SDK started before the turn
        return await helpers.delegate([{"goal": "find A"}], Ctx())

    def test_one_failure_does_not_sink_the_others(self):
        report = self.delegate(["find A", "fail please", "find C"])
        self.assertEqual(self.statuses(), ["done", "failed", "done"])
        self.assertIn("model down", report)
        self.assertIn("Found it for: find C", report)

    def test_bad_requests_are_refused_before_anything_runs(self):
        self.assertIn("Not run: give 1 to 3 jobs", asyncio.run(helpers.delegate([{"goal": "x"}] * 4, Ctx())))
        self.assertIn("Not run: every job needs a goal", asyncio.run(helpers.delegate([{"context": "x"}], Ctx())))
        self.assertEqual(FakeBrain.started, 0)


class LimitTests(Base):
    def test_a_helper_past_its_time_is_stopped_and_keeps_what_it_had(self):
        with patch.object(helpers, "HELPER_SECONDS", 0.3):
            report = self.delegate(["slow job"])
        self.assertEqual(self.statuses(), ["timed out"])
        self.assertIn("partial findings", report)

    def test_a_helper_over_its_token_budget_is_stopped(self):
        report = self.delegate(["huge job"])
        self.assertEqual(self.statuses(), ["over budget"])
        self.assertIn("partial findings", report)
        self.assertIn("40,000 tokens", report)

    def test_no_more_than_three_run_at_once_across_calls(self):
        async def go():
            FakeBrain.gate = asyncio.Event()
            first = asyncio.create_task(helpers.delegate([{"goal": "slow 1"}, {"goal": "slow 2"}], Ctx("chat-a")))
            second = asyncio.create_task(helpers.delegate([{"goal": "slow 3"}, {"goal": "slow 4"}], Ctx("chat-b")))
            for _ in range(200):
                if FakeBrain.started >= 3:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            self.assertEqual(sorted(self.statuses()), ["running", "running", "running", "waiting"])
            FakeBrain.gate.set()
            await asyncio.gather(first, second)

        asyncio.run(go())
        self.assertEqual((FakeBrain.started, FakeBrain.most_at_once), (4, 3))
        self.assertEqual(self.statuses(), ["done"] * 4)

    def test_a_slow_batch_answers_in_time_and_is_collected_later(self):
        ctx = Ctx()

        async def go():
            FakeBrain.gate = asyncio.Event()
            with patch.object(helpers, "WAIT_SECONDS", 0.2):
                report = await helpers.delegate([{"goal": "slow research"}], ctx)
            self.assertIn("still working", report)
            self.assertIn("helper_results", report)
            FakeBrain.gate.set()
            for _ in range(200):
                if self.statuses() == ["done"]:
                    break
                await asyncio.sleep(0.01)

        asyncio.run(go())
        later = helpers.results_text(None, Ctx())
        self.assertIn("Found it for: slow research", later)
        self.assertNotIn("still working", later)


class StopTests(Base):
    def test_stopping_the_turn_that_asked_stops_its_helpers(self):
        async def go():
            FakeBrain.gate = asyncio.Event()
            asking = asyncio.create_task(helpers.delegate([{"goal": "slow A"}, {"goal": "slow B"}], Ctx()))
            for _ in range(200):
                if FakeBrain.started >= 2:
                    break
                await asyncio.sleep(0.01)
            asking.cancel()
            await asyncio.gather(asking, return_exceptions=True)
            await until_statuses(self, ["stopped", "stopped"])

        asyncio.run(go())
        self.assertEqual(FakeBrain.active, 0)
        self.assertIn("partial findings", helpers.results_text(None, Ctx()))

    def test_stopping_a_chat_stops_helpers_that_outlived_the_tool_call(self):
        async def go():
            FakeBrain.gate = asyncio.Event()
            with patch.object(helpers, "WAIT_SECONDS", 0.2):
                await helpers.delegate([{"goal": "slow A"}], Ctx("chat-9"))
            self.assertEqual(helpers.stop_parent("chat:chat-9"), 1)
            await until_statuses(self, ["stopped"])

        asyncio.run(go())


class RestartTests(Base):
    def test_unfinished_helpers_are_lost_never_rerun_and_finished_ones_stay(self):
        self.delegate(["find A"])
        with store.transaction() as conn:
            batch = conn.execute("SELECT batch_id FROM helpers").fetchone()["batch_id"]
            for n, status in enumerate(("waiting", "running")):
                conn.execute("INSERT INTO helpers (id, batch_id, parent, goal, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                             (f"cut{n}", batch, "chat:chat-1", f"cut off {n}", status, 9e9 + n))
        started = FakeBrain.started
        self.assertEqual(helpers.recover(), 2)
        self.assertEqual(self.statuses(), ["done", "lost", "lost"])
        report = helpers.results_text(batch, Ctx())
        self.assertIn("Found it for: find A", report)
        self.assertIn("Kairos closed while it worked", report)
        self.assertEqual(FakeBrain.started, started, "nothing ran again")


class CollectTests(Base):
    def test_a_chat_reads_only_its_own_helpers(self):
        self.delegate(["mine"], Ctx("chat-1"))
        self.delegate(["theirs"], Ctx("chat-2"))
        with store.transaction() as conn:
            theirs = conn.execute("SELECT batch_id FROM helpers WHERE parent = 'chat:chat-2'").fetchone()["batch_id"]
        self.assertIn("Found it for: mine", helpers.results_text(None, Ctx("chat-1")))
        self.assertIn("No helpers with that batch id", helpers.results_text(theirs, Ctx("chat-1")))
        self.assertIn("has not handed any work", helpers.results_text(None, Ctx("chat-3")))

    def test_a_tasks_helpers_belong_to_the_task_and_stop_with_it(self):
        async def go():
            FakeBrain.gate = asyncio.Event()
            token = runs.CURRENT.set(runs.RunContext("task", task_id="t-42"))
            try:
                with patch.object(helpers, "WAIT_SECONDS", 0.2):
                    await helpers.delegate([{"goal": "slow task job"}], Ctx(session_id=None))
            finally:
                runs.CURRENT.reset(token)
            from core import task_scheduler
            with patch.object(task_scheduler, "_run_task", side_effect=asyncio.CancelledError), \
                 patch.object(task_scheduler.task_service, "record_stopped"):
                await task_scheduler.start_run({"id": "t-42", "name": "A task", "schedule_kind": "card"}, "manual")
            await until_statuses(self, ["stopped"])  # stopping the task stopped its helper

        asyncio.run(go())
        with store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT parent FROM helpers").fetchone()["parent"], "task:t-42")

    def test_deleting_a_chat_deletes_its_helpers(self):
        self.delegate(["mine"], Ctx("chat-gone"))
        store.delete_session("chat-gone")
        self.assertEqual(self.statuses(), [])


class ScopeTests(unittest.TestCase):
    def test_a_helper_brain_has_only_read_tools_and_the_browser(self):
        brain = ExternalBrain("http://127.0.0.1:1", "m", None, num_ctx=8192, window=8192, helper=True)
        names = {t["function"]["name"] for t in brain.tools}
        self.assertIn("search_vault", names)
        self.assertIn("browse", names)
        self.assertIn("search_sessions", names, "nothing is hidden behind the bridge, even on a small window")
        for forbidden in ("delegate", "helper_results", "create_note", "update_task", "run_code", "run_shell",
                          "google_drive", "agent_ask", "jarvis_tool_call"):
            self.assertNotIn(forbidden, names)
        self.assertEqual(brain._messages[0]["content"], system_prompt.HELPER_PROMPT)
        self.assertTrue(brain._context().helper)

    def test_a_helper_naming_a_write_tool_is_refused(self):
        ctx = tool_registry.ToolContext(helper=True)
        for name in ("create_note", "delegate", "run_code"):
            self.assertEqual(asyncio.run(tool_registry.call(name, {"title": "x", "content": "y"}, ctx, tool_registry.OPENAI)),
                             f"Unknown tool: {name}")

    def test_every_chat_kind_can_delegate_but_a_helper_cannot(self):
        for surface in (tool_registry.CLAUDE, tool_registry.OPENAI, tool_registry.CODEX):
            self.assertIn("delegate", [s.name for s in tool_registry.specs(surface)])
        self.assertNotIn("delegate", [s.name for s in tool_registry.specs(tool_registry.OPENAI, helper=True)])


class SettingTests(unittest.TestCase):
    def setUp(self):
        self.created = []
        self.addCleanup(lambda: [model_endpoints.delete_endpoint(e) for e in self.created])
        self.addCleanup(lambda: settings_store.update_settings(helper_endpoint_id=None))
        app = FastAPI()
        app.include_router(settings_routes.router)
        app.dependency_overrides[require_admin] = lambda: "david"
        self.web = TestClient(app)

    def endpoint(self, kind, name):
        ep = model_endpoints.create_endpoint(name, "http://127.0.0.1:1" if kind in ("local", "api") else "", "m", kind=kind)
        self.created.append(ep["id"])
        return ep

    def test_the_default_is_the_first_local_model_and_off_means_off(self):
        self.assertIsNone(helpers.helper_endpoint(), "no local model, no helpers")
        self.endpoint("claude_cli", "Claude")
        api = self.endpoint("api", "Paid API")
        self.assertIsNone(helpers.helper_endpoint(), "never a paid API model unless chosen")
        local = self.endpoint("local", "Local")
        self.assertEqual(helpers.helper_endpoint()["id"], local["id"])
        self.assertEqual(self.web.post("/api/settings/helpers", json={"endpoint_id": api["id"]}).status_code, 200)
        self.assertEqual(helpers.helper_endpoint()["id"], api["id"])
        self.web.post("/api/settings/helpers", json={"endpoint_id": "off"})
        self.assertIsNone(helpers.helper_endpoint())
        self.assertIn("helpers are off", asyncio.run(helpers.delegate([{"goal": "x"}], Ctx())))
        self.assertEqual(self.web.get("/api/settings").json()["helper_endpoint_id"], "off")

    def test_claude_and_codex_cannot_be_the_helper_model(self):
        claude = self.endpoint("claude_cli", "Claude")
        self.assertEqual(self.web.post("/api/settings/helpers", json={"endpoint_id": claude["id"]}).status_code, 400)
        self.assertEqual(self.web.post("/api/settings/helpers", json={"endpoint_id": "nope"}).status_code, 400)


def tearDownModule():
    store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
