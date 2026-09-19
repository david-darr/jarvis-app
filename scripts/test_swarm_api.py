"""Real Swarm router/service with isolated auth and temporary SQLite data."""
import asyncio
import ast
from contextlib import asynccontextmanager
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
environment = tempfile.TemporaryDirectory(prefix="jarvis-swarm-api-")
os.environ["JARVIS_DATA_DIR"] = environment.name

import httpx
from fastapi import FastAPI
from core import middleware
from core.swarm.budget import BudgetLimit
from core.swarm.store import Conflict, SCHEMA, SwarmStore
from routes import swarm_routes
from services.swarm_service import SwarmService


def setup_body(command="create"):
    limit = {"ceiling": 10000, "pause_percent": 80, "checkpoint_reserve": 100}
    return {"command_id": command, "name": "App company", "mission": "Build the app",
            "mode": "autonomous", "system_limit": limit, "run_limit": limit,
            "pool_limit": limit, "lead": {"name": "PM", "role": "Lead", "limit": limit},
            "specialists": [{"name": "Backend", "role": "Developer", "limit": limit}]}


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=environment.name)
        self.service = SwarmService(self.directory.name)
        await self.service.start()
        self.assertIsNone(self.service.fault)
        self.app = FastAPI()
        self.app.include_router(swarm_routes.router)
        self.app.state.swarm = self.service
        self.auth = patch("core.middleware.auth_enabled", return_value=True)
        self.auth.start()
        self.tokens = patch.object(middleware.auth_manager, "validate_session",
                                   side_effect=lambda token: {"a": "alice", "b": "bob"}.get(token))
        self.token_mock = self.tokens.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.close()
        self.tokens.stop()
        self.auth.stop()
        self.directory.cleanup()

    async def call(self, method, path, *, user="a", body=None):
        headers = {"Cookie": f"{middleware.SESSION_COOKIE_NAME}={user}"} if user else {}
        return await self.client.request(method, "/api/swarm" + path, json=body, headers=headers)

    async def create(self, user="a", command="create", **changes):
        body = {**setup_body(command), **changes}
        response = await self.call("POST", "/systems", user=user, body=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    async def snapshot(self, system, user="a"):
        response = await self.call("GET", f"/systems/{system}", user=user)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def action(self, system, action, command=None, revision=None, user="a"):
        if revision is None:
            revision = (await self.snapshot(system, user))["system"]["revision"]
        return await self.call("POST", f"/systems/{system}/{action}", user=user,
                               body={"command_id": command or action, "expected_revision": revision})

    async def test_authentication_and_internal_tool_not_human(self):
        self.assertEqual((await self.call("GET", "/systems", user=None)).status_code, 401)
        response = await self.client.get("/api/swarm/systems", headers={"X-JARVIS-Internal-Token": middleware.INTERNAL_TOOL_TOKEN})
        self.assertEqual(response.status_code, 403)
        with patch("core.middleware.auth_enabled", return_value=False):
            self.assertEqual((await self.call("GET", "/systems", user=None)).status_code, 200)

    async def test_create_persists_team_mode_and_idempotency(self):
        system = await self.create()
        self.assertEqual(system, await self.create())
        snapshot = await self.snapshot(system)
        self.assertEqual(snapshot["system"]["owner"], "alice")
        self.assertEqual(snapshot["system"]["configuration"]["mode"], "autonomous")
        self.assertEqual(len(snapshot["agents"]), 2)
        self.assertEqual(sum(a["is_lead"] for a in snapshot["agents"]), 1)
        # C2: a snapshot carries this company's own verdict. With no
        # connections assigned it is honestly blocked, while the coordinator
        # itself is ready - see test_status_names_the_teammate_that_cannot_run.
        self.assertFalse(snapshot["availability"]["execution_available"])
        self.assertEqual(snapshot["availability"]["code"], "setup_blocked")
        self.assertEqual((await self.call("POST", "/systems", body={**setup_body(), "name": "Changed"})).status_code, 409)
        self.assertEqual((await self.call("GET", "/systems")).json()["total"], 1)

    async def test_snapshot_dependencies_are_scoped_to_the_system(self):
        """The work graph's edges: both ends must resolve inside this system."""
        store = self.service.store
        limit = BudgetLimit(ceiling=10000, pause_percent=80, checkpoint_reserve=100)
        systems = [await self.create(), await self.create(command="second", name="Second company")]
        tasks = {}
        for system in systems:
            lead = store.db.execute("SELECT id FROM agents WHERE system_id=? AND is_lead=1", (system,)).fetchone()["id"]
            run = store.create_run(system, "First milestone", limit)
            first = store.create_task(system, run, lead, "Write the API contract", command_id=f"contract-{system}")
            second = store.create_task(system, run, lead, "Build the endpoint", command_id=f"endpoint-{system}",
                                       dependencies=[first])
            tasks[system] = (first, second)
        mine, theirs = systems
        own_edge = [{"task_id": tasks[mine][1], "depends_on": tasks[mine][0]}]
        self.assertEqual((await self.snapshot(mine))["dependencies"], own_edge)
        with self.assertRaises(Conflict):
            store.add_dependency(tasks[mine][1], tasks[theirs][0])
        # Defence in depth: a row written straight past that rejection must
        # still never reach the graph or leak another system's task id.
        store.db.execute("INSERT INTO dependencies VALUES(?,?)", (tasks[mine][1], tasks[theirs][0]))
        snapshot = await self.snapshot(mine)
        self.assertEqual(snapshot["dependencies"], own_edge)
        self.assertNotIn(tasks[theirs][0], json.dumps(snapshot))
        self.assertEqual((await self.snapshot(theirs))["dependencies"],
                         [{"task_id": tasks[theirs][1], "depends_on": tasks[theirs][0]}])

    async def test_status_names_the_teammate_that_cannot_run(self):
        """A company's own verdict reaches its snapshot, by name."""
        system = await self.create()
        availability = (await self.snapshot(system))["availability"]
        self.assertFalse(availability["execution_available"])
        self.assertEqual(availability["code"], "setup_blocked")
        self.assertIn("PM has no model connection.", availability["blockers"])
        self.assertIn("Backend has no model connection.", availability["blockers"])
        # The global reading stays about the coordinator, not any one team.
        self.assertTrue(self.service.status()["execution_available"])
        self.assertEqual(self.service.status()["blockers"], [])

    async def test_reconcile_requires_evidence_and_an_unknown_worker(self):
        """The only route out of an unconfirmed stop, and its conditions."""
        system = await self.create()
        store = self.service.store
        lead = store.db.execute("SELECT id FROM agents WHERE system_id=? AND is_lead=1", (system,)).fetchone()["id"]
        run = store.create_run(system, "Milestone", BudgetLimit(ceiling=10000, pause_percent=80, checkpoint_reserve=100))
        task = store.create_task(system, run, lead, "Draft something", command_id="task")
        runtime_id = self.service.runtime.runtime_id
        generation = self.service.runtime.generation
        attempt = store.reserve(task, runtime_id, generation, 500)
        store.begin(attempt, runtime_id, generation)
        store.worker_event(attempt, runtime_id, generation, "a1", "action_started",
                           {"action_id": "act-1", "intent": {"tool": "send_message"}})
        store.interrupt(attempt, runtime_id, generation, stopped=False, reason="company_pause")
        self.assertEqual(store.get_attempt(attempt)["state"], "unknown")
        self.assertEqual(store.get_task(task)["state"], "blocked")

        path = f"/systems/{system}/attempts/{attempt}/reconcile"
        self.assertEqual((await self.call("POST", path, body={"command_id": "r0", "evidence": "  "})).status_code, 422)
        self.assertEqual((await self.call("POST", path, user="b", body={"command_id": "r1", "evidence": "checked"})).status_code, 404)
        response = await self.call("POST", path, body={"command_id": "r2", "evidence": "No claude process is running."})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(store.get_task(task)["state"], "ready", "the held task is released")
        self.assertEqual(store.get_attempt(attempt)["state"], "cancelled")
        # The open tool action is closed as unknown, never given an outcome.
        action = store.db.execute("SELECT result FROM actions WHERE attempt_id=?", (attempt,)).fetchone()[0]
        self.assertIn("unknown", action)
        # The conservative hold ends with the uncertainty it stood for.
        settled = store.get_attempt(attempt)
        self.assertEqual(settled["held"], 0, "a reconciled attempt must not keep reserving capacity")
        self.assertEqual(settled["usage_complete"], 1)
        # And it cannot be done twice.
        self.assertEqual((await self.call("POST", path, body={"command_id": "r3", "evidence": "again"})).status_code, 409)

    async def test_a_stuck_worker_stays_reachable_behind_newer_attempts(self):
        """The attempts page holds fifty. A held task must not become invisible."""
        roomy = {"ceiling": 1000000, "pause_percent": 99, "checkpoint_reserve": 0}
        body = setup_body("stuck-create")
        body["lead"] = {**body["lead"], "limit": roomy}
        body["specialists"] = [{**body["specialists"][0], "limit": roomy}]
        body["system_limit"] = body["pool_limit"] = roomy
        response = await self.call("POST", "/systems", body=body)
        self.assertEqual(response.status_code, 201, response.text)
        system = response.json()["id"]
        store = self.service.store
        runtime_id, generation = self.service.runtime.runtime_id, self.service.runtime.generation
        agents = {row["name"]: row["id"] for row in store.team(system)}
        run = store.create_run(system, "Milestone", BudgetLimit(ceiling=1000000, pause_percent=99, checkpoint_reserve=0))

        def revive():
            store.db.execute("UPDATE systems SET state='active',reason=NULL WHERE id=?", (system,))
            store.db.execute("UPDATE runs SET state='running' WHERE id=?", (run,))

        stuck_task = store.create_task(system, run, agents["PM"], "The stuck one", command_id="stuck")
        stuck = store.reserve(stuck_task, runtime_id, generation, 500)
        store.begin(stuck, runtime_id, generation)
        store.interrupt(stuck, runtime_id, generation, stopped=False, reason="company_pause")
        revive()
        # Bury it behind more than a page of newer attempts from another agent:
        # one agent may only hold a single unresolved attempt at a time.
        for index in range(55):
            task = store.create_task(system, run, agents["Backend"], f"Later work {index}", command_id=f"later-{index}")
            attempt = store.reserve(task, runtime_id, generation, 500)
            self.assertIsNotNone(attempt, f"filler attempt {index} should claim")
            store.begin(attempt, runtime_id, generation)
            store.interrupt(attempt, runtime_id, generation, stopped=True, reason="company_pause")
            revive()

        snapshot = await self.snapshot(system)
        self.assertNotIn(stuck, [item["id"] for item in snapshot["pages"]["attempts"]["items"]],
                         "the stuck attempt must actually be off its page for this to prove anything")
        self.assertEqual([item["id"] for item in snapshot["unknown_attempts"]], [stuck],
                         "the held worker is still offered for reconciliation")

    async def test_drafting_a_team_needs_a_real_connection_and_creates_nothing(self):
        """Opt-in, paid for by a named connection, and only ever a proposal."""
        before = self.service.store.db.execute("SELECT COUNT(*) FROM systems").fetchone()[0]
        missing = await self.call("POST", "/draft-team", body={"description": "Launch a channel",
                                                               "endpoint_id": "nope"})
        self.assertEqual(missing.status_code, 404)

        endpoint = {"id": "e1", "name": "Stub", "kind": "api", "base_url": "http://stub/v1",
                    "model": "m", "api_key": None, "num_ctx": None}
        drafted = {"rationale": "Small team.",
                   "lead": {"name": "Mo", "role": "Producer", "instructions": "Coordinates."},
                   "specialists": [{"name": "Rae", "role": "Researcher", "instructions": "Finds angles."}]}

        async def fake_draft(_endpoint, goal):
            self.assertIn("channel", goal)
            return drafted

        with patch.object(SwarmService, "_resolved", staticmethod(lambda endpoint_id: endpoint)), \
             patch("services.swarm_service.architect.draft", fake_draft):
            response = await self.call("POST", "/draft-team",
                                       body={"description": "Launch a channel", "endpoint_id": "e1"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["lead"]["name"], "Mo")
        self.assertEqual(self.service.store.db.execute("SELECT COUNT(*) FROM systems").fetchone()[0], before,
                         "a draft is a proposal; nothing is created until the form is saved")
        self.assertEqual((await self.call("POST", "/draft-team", user=None,
                                          body={"description": "x", "endpoint_id": "e1"})).status_code, 401)

    async def test_a_draft_that_fails_says_so_rather_than_half_filling(self):
        from core.swarm.architect import DraftFailed

        async def refuse(_endpoint, _goal):
            raise DraftFailed("The model returned nothing.")

        endpoint = {"id": "e1", "name": "Stub", "kind": "api", "base_url": "http://stub/v1",
                    "model": "m", "api_key": None, "num_ctx": None}
        with patch.object(SwarmService, "_resolved", staticmethod(lambda endpoint_id: endpoint)), \
             patch("services.swarm_service.architect.draft", refuse):
            response = await self.call("POST", "/draft-team",
                                       body={"description": "Launch a channel", "endpoint_id": "e1"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("returned nothing", response.json()["detail"])

    async def test_foreign_system_all_surfaces_not_found(self):
        system = await self.create()
        checkpoint = (await self.call("POST", f"/systems/{system}/checkpoints", body={"command_id": "save"})).json()["id"]
        for suffix in ("", "/messages", "/tasks", "/runs", "/attempts", "/activity", "/checkpoints", "/events", f"/checkpoints/{checkpoint}/download"):
            response = await self.call("GET", f"/systems/{system}{suffix}", user="b")
            self.assertEqual(response.status_code, 404, suffix)
        for suffix, body in (("messages", {"command_id": "message", "body": "attack"}),
                             ("checkpoints", {"command_id": "save"}),
                             ("stop", {"command_id": "stop", "expected_revision": 0})):
            self.assertEqual((await self.call("POST", f"/systems/{system}/{suffix}", user="b", body=body)).status_code, 404)
        self.assertEqual((await self.call("GET", "/systems", user="b")).json()["items"], [])
        self.assertEqual((await self.call("GET", "/pools", user="b")).json(), [])

    async def test_foreign_pool_and_spoofed_owner_are_rejected_atomically(self):
        system = await self.create()
        pool = (await self.snapshot(system))["agents"][0]["pool_id"]
        before = self.service.store.db.execute("SELECT COUNT(*) FROM systems").fetchone()[0]
        response = await self.call("POST", "/systems", user="b", body={**setup_body(), "pool_id": pool})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.service.store.db.execute("SELECT COUNT(*) FROM systems").fetchone()[0], before)
        for change in ({"owner": "alice"}, {"system_limit": {"ceiling": True}}, {"mission": "  "}, {"fake_worker": True}):
            self.assertEqual((await self.call("POST", "/systems", user="b", body={**setup_body(), **change})).status_code, 422)

    async def test_failed_create_rolls_back_pool_system_agents_and_command(self):
        with patch.object(self.service.store, "_team_member", side_effect=ValueError("invalid team")):
            response = await self.call("POST", "/systems", body=setup_body())
        self.assertEqual(response.status_code, 422)
        for table in ("systems", "pools", "agents", "commands"):
            self.assertEqual(self.service.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    async def test_edit_revision_and_foreign_agent_checks(self):
        system = await self.create()
        other = await self.create(command="other")
        snapshot = await self.snapshot(system)
        body = setup_body("edit")
        body["expected_revision"] = snapshot["system"]["revision"]
        body["lead"]["id"] = snapshot["agents"][0]["id"]
        body["specialists"][0]["id"] = snapshot["agents"][1]["id"]
        invalid = copy.deepcopy(body)
        invalid["specialists"][0]["id"] = (await self.snapshot(other))["agents"][1]["id"]
        self.assertEqual((await self.call("PATCH", f"/systems/{system}", body=invalid)).status_code, 404)
        body["name"] = "Renamed"
        self.assertEqual((await self.call("PATCH", f"/systems/{system}", body=body)).status_code, 200)
        self.assertEqual((await self.call("PATCH", f"/systems/{system}", body=body)).status_code, 200)
        body["command_id"] = "stale"
        self.assertEqual((await self.call("PATCH", f"/systems/{system}", body=body)).status_code, 409)
        self.assertEqual((await self.snapshot(system))["system"]["name"], "Renamed")

    async def test_queued_messages_and_cursor_pagination(self):
        system = await self.create()
        first = (await self.snapshot(system))["event_cursor"]
        for i in range(4):
            body = {"command_id": str(i), "body": f"Idea {i}"}
            for _ in range(2):
                self.assertEqual((await self.call("POST", f"/systems/{system}/messages", body=body)).status_code, 202)
        result = (await self.call("GET", f"/systems/{system}/messages?limit=2&offset=2")).json()
        self.assertEqual(result["total"], 4)
        self.assertEqual(len(result["items"]), 2)
        events = self.service.store.owner_events("alice", system, first)
        self.assertEqual(len(events), 4)
        self.assertEqual(self.service.store.owner_events("alice", system, events[-1]["id"]), [])

    async def test_start_and_resume_cannot_fake_execution(self):
        """A team with no usable connection is refused, by name, and nothing runs."""
        system = await self.create()
        for action in ("start", "resume"):
            response = await self.action(system, action)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "setup_blocked")
            self.assertIn("PM has no model connection", response.json()["detail"])
        self.assertEqual((await self.snapshot(system))["system"]["state"], "idle")
        self.assertEqual(self.service.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.service.cycles, {})

    async def test_pause_stop_archive_restore_and_retry(self):
        system = await self.create()
        self.assertEqual((await self.action(system, "pause")).status_code, 200)
        snapshot = await self.snapshot(system)
        self.assertEqual(snapshot["system"]["state"], "paused")
        self.assertGreater(snapshot["pages"]["checkpoints"]["total"], 0)
        revision = snapshot["system"]["revision"]
        self.assertEqual((await self.action(system, "stop", revision=revision)).status_code, 200)
        self.assertEqual((await self.action(system, "stop", revision=revision)).status_code, 200)
        self.assertEqual((await self.snapshot(system))["system"]["state"], "stopped")
        self.assertEqual((await self.action(system, "archive")).status_code, 200)
        self.assertEqual((await self.call("POST", f"/systems/{system}/messages", body={"command_id": "x", "body": "Idea"})).status_code, 409)
        self.assertEqual((await self.call("POST", f"/systems/{system}/checkpoints", body={"command_id": "archive-save"})).status_code, 409)
        self.assertEqual((await self.call("GET", f"/systems/{system}")).headers["cache-control"], "no-store")
        self.assertEqual((await self.action(system, "restore")).status_code, 200)

    async def test_stop_preserves_unknown_worker_and_prevents_archive(self):
        system = await self.create()
        snapshot = await self.snapshot(system)
        store, runtime = self.service.store, self.service.runtime
        run = store.create_run(system, "Test", BudgetLimit(10000))
        task = store.create_task(system, run, snapshot["agents"][0]["id"], "Test", command_id="task")
        attempt = store.reserve(task, runtime.runtime_id, runtime.generation, 10)
        store.begin(attempt, runtime.runtime_id, runtime.generation)
        store.interrupt(attempt, runtime.runtime_id, runtime.generation, stopped=False, reason="unknown")
        runtime._observe()
        self.assertEqual((await self.action(system, "stop")).status_code, 200)
        self.assertEqual(store.get_attempt(attempt)["state"], "unknown")
        self.assertEqual(store.get_task(task)["state"], "blocked")
        self.assertEqual((await self.action(system, "archive")).status_code, 409)

    async def test_quota_update_cannot_reactivate_stopped_or_archived_company(self):
        system = await self.create()
        pool = (await self.snapshot(system))["agents"][0]["pool_id"]
        await self.action(system, "stop")
        for expected in ("stopped", "archived"):
            if expected == "archived":
                await self.action(system, "archive")
            self.service.store.update_quota(pool, expected, used_percent=90, status="warning",
                                            observed_at=self.service.store.clock(), valid_until=self.service.store.clock() + 60)
            self.assertEqual((await self.snapshot(system))["system"]["state"], expected)

    async def test_removed_specialist_limit_does_not_block_but_past_usage_remains(self):
        data = setup_body()
        data["specialists"][0]["limit"] = {"ceiling": 10, "pause_percent": 80, "checkpoint_reserve": 0}
        response = await self.call("POST", "/systems", body=data)
        system = response.json()["id"]
        snapshot = await self.snapshot(system)
        store, runtime = self.service.store, self.service.runtime
        run = store.create_run(system, "Small task", BudgetLimit(10000))
        task = store.create_task(system, run, snapshot["agents"][1]["id"], "Small task", command_id="small")
        attempt = store.reserve(task, runtime.runtime_id, runtime.generation, 8)
        store.begin(attempt, runtime.runtime_id, runtime.generation)
        store.record_usage(attempt, "call", 8)
        store.finish(attempt, runtime.runtime_id, runtime.generation, {"done": True}, usage_complete=True)
        store.accept_result(task, expected_revision=store.get_task(task)["revision"], evidence="Fixture check")
        runtime._observe()
        snapshot = await self.snapshot(system)
        data.update(command_id="remove", expected_revision=snapshot["system"]["revision"], specialists=[])
        data["lead"]["id"] = snapshot["agents"][0]["id"]
        self.assertEqual((await self.call("PATCH", f"/systems/{system}", body=data)).status_code, 200)
        # A completed run leaves no paused run to check; start another internal
        # fixture run to exercise admission and retain cumulative accounting.
        store.resume(system)
        run = store.create_run(system, "Next task", BudgetLimit(10000))
        task = store.create_task(system, run, data["lead"]["id"], "Next", command_id="next")
        self.assertIsNotNone(store.reserve(task, runtime.runtime_id, runtime.generation, 8))
        total = next(b for b in (await self.snapshot(system))["budgets"] if b["scope"] == "system")
        self.assertEqual(total["used"], 8)

    async def test_download_is_selected_snapshot_private_and_read_only(self):
        system = await self.create()
        body = {"command_id": "checkpoint"}
        first = (await self.call("POST", f"/systems/{system}/checkpoints", body=body)).json()
        self.assertEqual(first, (await self.call("POST", f"/systems/{system}/checkpoints", body=body)).json())
        await self.call("POST", f"/systems/{system}/messages", body={"command_id": "later", "body": "Later idea"})
        path = f"/systems/{system}/checkpoints/{first['id']}/download"
        response = await self.call("GET", path + "?format=json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"], [])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual((await self.call("GET", path + "?format=md")).status_code, 200)
        self.assertEqual((await self.snapshot(system))["pages"]["checkpoints"]["total"], 1)
        other = await self.create(command="other")
        self.assertEqual((await self.call("GET", path.replace(system, other))).status_code, 404)
        self.assertEqual((await self.call("GET", path + "?format=html")).status_code, 422)

    async def test_sse_replay_and_session_revocation(self):
        system = await self.create()
        class Request:
            headers = {"last-event-id": "0"}
            cookies = {middleware.SESSION_COOKIE_NAME: "a"}
            async def is_disconnected(self):
                return False
        request = Request()
        response = await swarm_routes.events(system, request, after=0, owner="alice", svc=self.service)
        iterator = response.body_iterator
        first = await anext(iterator)
        self.assertIn("event: change", first)
        self.assertIn("id:", first)
        self.token_mock.side_effect = lambda token: None
        revoked = await asyncio.wait_for(anext(iterator), 2)
        self.assertIn("event: unavailable", revoked)
        await iterator.aclose()

    async def test_fault_status_does_not_report_available_or_admit_writes(self):
        self.service.fault = RuntimeError("fixture storage fault")
        status = (await self.call("GET", "/status")).json()
        self.assertFalse(status["available"])
        self.assertEqual((await self.call("POST", "/systems", body=setup_body())).status_code, 503)


class MigrationTests(unittest.TestCase):
    def test_version_one_data_and_ambiguous_pool_ownership(self):
        with tempfile.TemporaryDirectory(dir=environment.name) as directory:
            path = Path(directory) / "legacy.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=1")
            db.executemany("INSERT INTO pools VALUES(?,?)", [("shared", "Shared"), ("owned", "Owned"), ("orphan", "Orphan")])
            db.executemany("INSERT INTO systems(id,owner,name,mission,created_at) VALUES(?,?,?,?,?)",
                           [("a", "alice", "A", "Mission A", 1), ("b", "bob", "B", "Mission B", 1)])
            db.executemany("INSERT INTO agents VALUES(?,?,?,?,?,?)", [("a1", "a", "Lead A", "lead", 1, "shared"),
                           ("b1", "b", "Lead B", "lead", 1, "shared"), ("a2", "a", "Dev", "dev", 0, "owned")])
            db.execute("INSERT INTO messages(id,system_id,recipient_id,body,created_at) VALUES('m','a','a1','Saved idea',1)")
            db.commit(); db.close()
            store = SwarmStore(path)
            try:
                self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 3)
                pools = {row["id"]: row["owner"] for row in store.db.execute("SELECT * FROM pools")}
                self.assertEqual(pools, {"shared": None, "owned": "alice", "orphan": None})
                self.assertEqual(store.owner_page("alice", "a", "messages")["items"][0]["body"], "Saved idea")
                self.assertTrue(store.owner_snapshot("alice", "a")["allocation_ownership_unresolved"])
            finally:
                store.close()


class AppWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_lifespan_closes_swarm_on_failure_without_starting_live_services(self):
        # Execute the actual wiring function with isolated collaborators. Do
        # not import app.py: its module-level work touches live app data.
        source = Path(__file__).resolve().parents[1] / "app.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == "lifespan")
        swarm = Mock(startup=AsyncMock(), shutdown=AsyncMock())
        scope = {"asynccontextmanager": asynccontextmanager, "FastAPI": FastAPI, "swarm_service": swarm,
                 "vault_sync": Mock(), "skills_service": Mock(), "migrate_builtin_schedules": Mock(),
                 "autoenable_builtins": Mock(), "task_scheduler": Mock(), "llamacpp_engine": Mock(),
                 "discord_channel": Mock(start=AsyncMock(), stop=AsyncMock()),
                 "remote_access": Mock(start_if_enabled=AsyncMock(), stop=AsyncMock()),
                 "chat_service": Mock(shutdown=AsyncMock())}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), scope)
        application = FastAPI()
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            async with scope["lifespan"](application):
                raise RuntimeError("fixture failure")
        swarm.startup.assert_awaited_once_with(application)
        swarm.shutdown.assert_awaited_once_with(application)
        scope["chat_service"].shutdown.assert_awaited_once()


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        environment.cleanup()
