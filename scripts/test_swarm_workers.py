"""Real adapters against stubs: no network, no provider, no model call.

What these prove: the tool surface is the boundary, usage is charged per call,
an endpoint that cannot do structured tools is refused instead of quietly
downgraded, prose is never executed as an action, a bounded step that runs out
is not success, and a whole delegate-work-review cycle completes through the
real service and runtime.

What they cannot prove, and no stub can: that a provider's rate-limit messages
arrive for a given account, that killing a CLI kills its descendants, or what
anything really costs. Those need the one live run.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
environment = tempfile.TemporaryDirectory(prefix="jarvis-swarm-workers-")
os.environ["JARVIS_DATA_DIR"] = environment.name

from core.swarm import accounts
from core.swarm.adapters import WorkerContext, build_worker, capability_for
from core.swarm.adapters.claude_worker import ClaudeWorker, _quota_event
from core.swarm.adapters.openai_worker import OpenAIWorker, UnsupportedEndpoint
from core.swarm.models import Assignment, EventKind
from core.swarm.tools import ToolRejected, ToolService
from core.swarm.store import SwarmStore
from services.swarm_service import SwarmService

LIMIT = {"ceiling": 20000, "pause_percent": 80, "checkpoint_reserve": 0}


def setup_payload(lead_endpoint="endpoint-a", specialist_endpoint="endpoint-a"):
    return {
        "name": "App studio", "mission": "Ship a small tracker", "mode": "guided",
        "system_limit": {"ceiling": 100000, "pause_percent": 80, "checkpoint_reserve": 0},
        "run_limit": LIMIT, "pool_limit": LIMIT, "pool_id": None,
        "lead": {"name": "PM", "role": "Lead", "instructions": "", "limit": LIMIT,
                 "endpoint_id": lead_endpoint, "model": None, "effort": None},
        "specialists": [{"name": "Backend", "role": "Developer", "instructions": "", "limit": LIMIT,
                         "endpoint_id": specialist_endpoint, "model": None, "effort": None}],
    }


class Scripted(BaseHTTPRequestHandler):
    """A stub /chat/completions. Replies are chosen by the caller's script."""
    script = None
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)) or "{}")
        Scripted.requests.append(body)
        status, payload = Scripted.script(body)
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


def reply(text=None, calls=None, usage=(10, 5)):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [{"id": f"call-{index}", "type": "function",
                                  "function": {"name": name, "arguments": json.dumps(arguments)}}
                                 for index, (name, arguments) in enumerate(calls)]
    return 200, {"choices": [{"message": message}],
                 "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


class StubServer:
    def __init__(self, script):
        Scripted.script = staticmethod(script)
        Scripted.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class Fixture:
    """A real store with a real company, built without any provider."""

    def __init__(self, root):
        self.store = SwarmStore(Path(root) / "swarm.sqlite3")
        payload = setup_payload()
        payload["lead"]["account_key"] = payload["specialists"][0]["account_key"] = "api:stub"
        payload["lead"]["account_label"] = payload["specialists"][0]["account_label"] = "Stub account"
        payload["lead"]["pool_limit"] = payload["specialists"][0]["pool_limit"] = LIMIT
        self.system_id = self.store.owner_create("alice", payload, "create")["id"]
        self.team = {agent["name"]: agent for agent in self.store.team(self.system_id)}
        from core.swarm.budget import BudgetLimit
        self.run_id = self.store.create_run(self.system_id, "Ship it", BudgetLimit(**LIMIT))

    def task(self, agent_name, objective="Do the thing"):
        created = self.store.create_plan(self.system_id, self.run_id, self.team["PM"]["id"], [
            {"key": "t", "agent_id": self.team[agent_name]["id"], "objective": objective}],
            command_id=f"plan-{agent_name}-{objective}")
        return created["t"]

    def assignment(self, agent_name, task_id, objective="Do the thing"):
        agent = self.team[agent_name]
        return Assignment("attempt-1", self.system_id, self.run_id, agent["id"], task_id,
                          objective, 5000, {}, agent["pool_id"])

    def service_for(self, agent_name):
        return ToolService(self.store, is_lead=bool(self.team[agent_name]["is_lead"]))

    def close(self):
        self.store.close()


class AccountTests(unittest.TestCase):
    def test_same_account_shares_a_key_and_never_exposes_the_secret(self):
        first = {"kind": "api", "base_url": "https://api.example.com/v1", "api_key": "sk-secret"}
        second = {"kind": "api", "base_url": "https://API.example.com/v1", "api_key": "sk-secret"}
        third = {"kind": "api", "base_url": "https://api.example.com/v1", "api_key": "sk-other"}
        self.assertEqual(accounts.account_key(first), accounts.account_key(second))
        self.assertNotEqual(accounts.account_key(first), accounts.account_key(third))
        self.assertNotIn("sk-secret", accounts.account_key(first))
        self.assertEqual(accounts.account_key({"kind": "claude_cli"}), "claude_cli")
        self.assertEqual(accounts.account_key({"kind": "codex_cli"}), "codex_cli")

    def test_capability_gates_admission_by_what_a_kind_can_do(self):
        self.assertIsNone(capability_for({"kind": "claude_cli"}).blocked_reason())
        self.assertIsNone(capability_for({"kind": "api"}).blocked_reason())
        codex = capability_for({"kind": "codex_cli"})
        self.assertFalse(codex.structured_tools)
        self.assertIn("structured tool", codex.blocked_reason())


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(dir=environment.name)
        self.fixture = Fixture(self.root.name)

    def tearDown(self):
        self.fixture.close()
        self.root.cleanup()

    def bound(self, agent_name):
        task_id = self.fixture.task(agent_name)
        service = self.fixture.service_for(agent_name)
        service.bind(self.fixture.assignment(agent_name, task_id))
        return service, task_id

    def test_specialist_cannot_assign_or_review_however_it_asks(self):
        service, _ = self.bound("Backend")
        self.assertNotIn("assign_plan", service.definitions)
        for name in ("assign_plan", "review_work", "delete_everything"):
            with self.assertRaises(ToolRejected):
                service.call(name, {"tasks": []})

    def test_messages_resolve_by_name_and_cannot_leave_the_company(self):
        service, _ = self.bound("Backend")
        service.call("send_message", {"to": "PM", "body": "What schema?"})
        delivered = self.fixture.store.inbox(self.fixture.team["PM"]["id"])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["body"], "What schema?")
        with self.assertRaises(ToolRejected):
            service.call("send_message", {"to": "Somebody Else", "body": "hello"})
        with self.assertRaises(ToolRejected):
            service.call("send_message", {"to": "Backend", "body": "talking to myself"})

    def test_assignment_context_carries_upstream_results_and_unread_mail(self):
        first = self.fixture.task("Backend", "Write the contract")
        self.fixture.store.db.execute("UPDATE tasks SET state='done',result=? WHERE id=?",
                                      (json.dumps({"status": "submitted", "output": "POST /users"}), first))
        created = self.fixture.store.create_plan(
            self.fixture.system_id, self.fixture.run_id, self.fixture.team["PM"]["id"],
            [{"key": "second", "agent_id": self.fixture.team["Backend"]["id"],
              "objective": "Build it", "depends_on": [first]}], command_id="dependent")
        self.fixture.store.agent_message(self.fixture.system_id, self.fixture.team["PM"]["id"],
                                         self.fixture.team["Backend"]["id"], "Use snake_case")
        service = self.fixture.service_for("Backend")
        service.bind(self.fixture.assignment("Backend", created["second"], "Build it"))
        text = service.call("get_assigned_work", {})
        self.assertIn("POST /users", text)
        self.assertIn("Use snake_case", text)
        self.assertIn("PM (lead)", text)

    def test_lead_plan_is_all_or_nothing_and_rejects_a_stranger(self):
        service, _ = self.bound("PM")
        with self.assertRaises(ToolRejected):
            service.call("assign_plan", {"tasks": [
                {"key": "a", "assignee": "Backend", "objective": "Real work"},
                {"key": "b", "assignee": "Nobody", "objective": "Impossible work"}]})
        self.assertEqual(self.fixture.store.db.execute(
            "SELECT COUNT(*) FROM tasks WHERE objective='Real work'").fetchone()[0], 0)

    def test_review_only_accepts_work_actually_waiting(self):
        task_id = self.fixture.task("Backend", "Submitted work")
        self.fixture.store.db.execute("UPDATE tasks SET state='review',result=? WHERE id=?",
                                      (json.dumps({"status": "submitted", "output": "done"}), task_id))
        service, _ = self.bound("PM")
        with self.assertRaises(ToolRejected):
            service.call("review_work", {"decisions": [{"task": "not-a-task", "verdict": "accept", "note": "ok"}]})
        service.call("review_work", {"decisions": [{"task": task_id, "verdict": "accept", "note": "Checked the output"}]})
        self.assertEqual(self.fixture.store.get_task(task_id)["state"], "done")

    def test_a_revision_returns_the_task_and_tells_its_owner_why(self):
        task_id = self.fixture.task("Backend", "Needs work")
        self.fixture.store.db.execute("UPDATE tasks SET state='review' WHERE id=?", (task_id,))
        service, _ = self.bound("PM")
        service.call("review_work", {"decisions": [{"task": task_id, "verdict": "revise", "note": "No tests"}]})
        self.assertEqual(self.fixture.store.get_task(task_id)["state"], "ready")
        inbox = self.fixture.store.inbox(self.fixture.team["Backend"]["id"])
        self.assertIn("No tests", inbox[0]["body"])


class OpenAIWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(dir=environment.name)
        self.fixture = Fixture(self.root.name)
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.close()
        self.fixture.close()
        self.root.cleanup()

    def worker(self, script, agent_name="Backend"):
        self.server = StubServer(script)
        task_id = self.fixture.task(agent_name)
        service = self.fixture.service_for(agent_name)
        agent = self.fixture.team[agent_name]
        context = WorkerContext(
            endpoint={"kind": "api", "base_url": self.server.base_url, "model": "stub", "api_key": None},
            agent=agent, system=self.fixture.store.get_system(self.fixture.system_id),
            tool_service=service, scratch_dir=self.root.name)
        return OpenAIWorker(context), self.fixture.assignment(agent_name, task_id)

    async def collect(self, worker, assignment):
        return [event async for event in worker.events(assignment)]

    async def test_a_submitted_result_charges_every_call_and_ends_the_step(self):
        def script(body):
            if any(message["role"] == "tool" for message in body["messages"]):
                return reply(text="done")
            return reply(calls=[("submit_result", {"summary": "Built it", "output": "The thing"})])

        worker, assignment = self.worker(script)
        events = await self.collect(worker, assignment)
        kinds = [event.kind for event in events]
        self.assertEqual(kinds.count(EventKind.ACTION_STARTED), 1)
        self.assertEqual(kinds.index(EventKind.ACTION_STARTED), kinds.index(EventKind.ACTION_FINISHED) - 1)
        usage = [event for event in events if event.kind == EventKind.USAGE]
        self.assertEqual([event.data["units"] for event in usage], [15])
        final = events[-1]
        self.assertEqual(final.kind, EventKind.RESULT)
        self.assertEqual(final.data["result"]["status"], "submitted")
        self.assertTrue(final.data["usage_complete"])

    async def test_every_round_is_charged_not_only_the_last(self):
        state = {"calls": 0}

        def script(_body):
            state["calls"] += 1
            if state["calls"] == 1:
                return reply(calls=[("get_assigned_work", {})], usage=(100, 20))
            return reply(calls=[("submit_result", {"summary": "s", "output": "o"})], usage=(7, 3))

        worker, assignment = self.worker(script)
        events = await self.collect(worker, assignment)
        usage = [event.data["units"] for event in events if event.kind == EventKind.USAGE]
        self.assertEqual(usage, [120, 10])
        identifiers = [event.event_id for event in events if event.kind == EventKind.USAGE]
        self.assertEqual(len(set(identifiers)), 2, "each charge needs its own id or the store dedupes it away")

    async def test_an_endpoint_without_structured_tools_is_refused_not_downgraded(self):
        def script(_body):
            return 400, {"error": "tools are not supported"}

        worker, assignment = self.worker(script)
        with self.assertRaises(UnsupportedEndpoint):
            await self.collect(worker, assignment)
        self.assertEqual(len(Scripted.requests), 1, "a refused endpoint is never retried without tools")

    async def test_tool_shaped_prose_is_never_executed(self):
        def script(_body):
            return reply(text=json.dumps({"name": "submit_result",
                                          "arguments": {"summary": "fake", "output": "fake"}}))

        worker, assignment = self.worker(script)
        events = await self.collect(worker, assignment)
        self.assertFalse([event for event in events if event.kind == EventKind.ACTION_STARTED])
        self.assertEqual(events[-1].data["result"]["status"], "incomplete")
        self.assertEqual(self.fixture.store.db.execute("SELECT COUNT(*) FROM tasks WHERE state='review'").fetchone()[0], 0)

    async def test_running_out_of_rounds_is_not_a_result(self):
        worker, assignment = self.worker(lambda _body: reply(text="still thinking"))
        events = await self.collect(worker, assignment)
        self.assertEqual(events[-1].kind, EventKind.RESULT)
        self.assertEqual(events[-1].data["result"]["status"], "incomplete")
        self.assertIn("round limit", events[-1].data["result"]["reason"])

    async def test_a_rejected_tool_call_is_reported_back_not_raised(self):
        state = {"calls": 0}

        def script(_body):
            state["calls"] += 1
            if state["calls"] == 1:
                return reply(calls=[("send_message", {"to": "Ghost", "body": "hi"})])
            return reply(calls=[("report_blocker", {"reason": "No such teammate"})])

        worker, assignment = self.worker(script)
        events = await self.collect(worker, assignment)
        finished = [event for event in events if event.kind == EventKind.ACTION_FINISHED]
        self.assertFalse(finished[0].data["result"]["ok"])
        self.assertIn("No teammate", finished[0].data["result"]["text"])
        self.assertEqual(events[-1].data["result"]["status"], "blocked")

    async def test_cancel_closes_the_transport(self):
        worker, _ = self.worker(lambda _body: reply(text="hi"))
        self.assertTrue(await worker.cancel())
        self.assertTrue(worker.cancelled)


class ClaudeScopingTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(dir=environment.name)
        self.fixture = Fixture(self.root.name)

    def tearDown(self):
        self.fixture.close()
        self.root.cleanup()

    def options(self):
        agent = self.fixture.team["Backend"]
        context = WorkerContext(endpoint={"kind": "claude_cli", "model": None}, agent=agent,
                                system=self.fixture.store.get_system(self.fixture.system_id),
                                tool_service=self.fixture.service_for("Backend"),
                                scratch_dir=self.root.name)
        return ClaudeWorker(context)._options(asyncio.Queue())

    def test_a_worker_gets_swarm_tools_and_nothing_else(self):
        options = self.options()
        self.assertTrue(all(name.startswith("mcp__swarm__") for name in options.allowed_tools))
        self.assertIn("Bash", options.disallowed_tools)
        self.assertIn("Write", options.disallowed_tools)
        self.assertIsNone(getattr(options, "add_dirs", None) or None)
        self.assertEqual(options.setting_sources, [])
        self.assertTrue(options.strict_mcp_config)
        self.assertEqual(options.cwd, self.root.name)
        self.assertIsInstance(options.system_prompt, str)
        self.assertNotIn("claude_code", str(options.system_prompt))
        self.assertGreater(options.max_turns, 0)

    def test_a_turn_ceiling_is_an_outcome_and_a_real_fault_is_not(self):
        """The fields David's failure threw away.

        Running out of turns is a bound this app set, so it ends the step
        rather than raising. An API error is a fault and still raises - with
        what the SDK actually said, instead of one generic sentence.
        """
        from core.swarm.adapters.claude_worker import _failure_detail, _is_bounded

        class Ended:
            subtype = "error_max_turns"
            stop_reason = None
            terminal_reason = None
            result = None
            num_turns = 8
            api_error_status = None
            errors = None

        self.assertTrue(_is_bounded(Ended()))
        detail = _failure_detail(Ended())
        self.assertIn("error_max_turns", detail)
        self.assertIn("8 turns", detail)

        class Failed(Ended):
            subtype = "error_during_execution"
            api_error_status = 529
            errors = ["overloaded_error"]

        self.assertFalse(_is_bounded(Failed()), "an API error is a fault, not a ceiling")
        detail = _failure_detail(Failed())
        self.assertIn("529", detail)
        self.assertIn("overloaded_error", detail)

    def test_quota_readings_scale_and_stay_honest_about_the_unknown(self):
        class Info:
            status = "allowed_warning"
            utilization = 0.83
            rate_limit_type = "five_hour"
            resets_at = 1789600000

        class Message:
            rate_limit_info = Info()

        event = _quota_event(Message())
        self.assertEqual(event.data["bucket"], "five_hour")
        self.assertAlmostEqual(event.data["used_percent"], 83.0)
        self.assertEqual(event.data["status"], "warning")
        self.assertEqual(event.data["resets_at"], 1789600000)

        Info.utilization = None
        Info.status = "rejected"
        event = _quota_event(Message())
        self.assertIsNone(event.data["used_percent"], "missing is unknown, never zero")
        self.assertEqual(event.data["status"], "rejected")


class CodexEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_internal_admin_token_never_reaches_a_worker(self):
        from core.swarm.adapters.codex_worker import CodexWorker
        root = tempfile.TemporaryDirectory(dir=environment.name)
        fixture = Fixture(root.name)
        try:
            captured = {}

            class FakeProcess:
                returncode = 0
                pid = 4242

                def __init__(self):
                    self.stdin = self
                    self.stdout = self

                def write(self, _data):
                    pass

                def write_eof(self):
                    pass

                async def readline(self):
                    return b""

                async def wait(self):
                    return 0

            async def fake_exec(*args, **kwargs):
                captured["env"] = kwargs.get("env") or {}
                captured["args"] = args
                return FakeProcess()

            context = WorkerContext(endpoint={"kind": "codex_cli", "model": None},
                                    agent=fixture.team["Backend"],
                                    system=fixture.store.get_system(fixture.system_id),
                                    tool_service=fixture.service_for("Backend"), scratch_dir=root.name)
            worker = CodexWorker(context)
            task_id = fixture.task("Backend")
            with patch("asyncio.create_subprocess_exec", fake_exec), \
                 patch("shutil.which", return_value="codex"), \
                 patch.dict(os.environ, {"JARVIS_INTERNAL_TOKEN": "super-secret"}):
                events = [event async for event in worker.events(fixture.assignment("Backend", task_id))]
            self.assertNotIn("JARVIS_INTERNAL_TOKEN", captured["env"])
            self.assertNotIn("super-secret", json.dumps(captured["env"]))
            self.assertIn(root.name, captured["args"])
            self.assertNotIn("--add-dir", captured["args"])
            self.assertEqual(events[-1].data["result"]["status"], "incomplete")
        finally:
            fixture.close()
            root.cleanup()


class CycleTests(unittest.IsolatedAsyncioTestCase):
    """The whole point of C1: real delegation, driven by a real service."""

    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory(dir=environment.name)
        self.server = None
        self.service = SwarmService(Path(self.root.name))
        await self.service.start()
        self.assertIsNone(self.service.fault)

    async def asyncTearDown(self):
        await self.service.close()
        if self.server:
            self.server.close()
        self.root.cleanup()

    def endpoint(self):
        return {"id": "endpoint-a", "name": "Stub", "kind": "api",
                "base_url": self.server.base_url, "model": "stub", "api_key": None, "num_ctx": None}

    async def test_lead_plans_specialist_works_lead_reviews(self):
        def script(body):
            system_prompt = body["messages"][0]["content"]
            is_lead = "the lead of" in system_prompt
            waiting = "waiting on your review" in json.dumps(body["messages"])
            if is_lead and not waiting and not any(m["role"] == "tool" for m in body["messages"]):
                return reply(calls=[("get_assigned_work", {})])
            if is_lead and waiting:
                task_id = json.dumps(body["messages"])
                identifier = task_id.split("Work waiting on your review (use review_work with these ids):")[1]
                identifier = identifier.split("- ")[1].split(" ")[0]
                return reply(calls=[("review_work", {"decisions": [
                    {"task": identifier, "verdict": "accept", "note": "Matches the objective"}]})])
            if is_lead:
                return reply(calls=[("assign_plan", {"tasks": [
                    {"key": "one", "assignee": "Backend", "objective": "Draft the schema"}]})])
            if any(m["role"] == "tool" for m in body["messages"]):
                return reply(calls=[("submit_result", {"summary": "Schema drafted",
                                                       "output": "users(id, email)"})])
            return reply(calls=[("get_assigned_work", {})])

        self.server = StubServer(script)
        endpoint = self.endpoint()
        with patch.object(SwarmService, "_resolved", staticmethod(lambda endpoint_id: endpoint)),              patch.object(SwarmService, "_connection", staticmethod(lambda endpoint_id: endpoint)):
            created = await self.service.create("alice", setup_payload(), "create")
            system_id = created["id"]
            await self.service.message("alice", system_id, "Build a tracker", "idea")
            revision = self.service.store.get_system(system_id)["revision"]
            result = await self.service.lifecycle("alice", system_id, "start", "go", revision)
            self.assertEqual(result["status"], "active", result)
            cycle = self.service.cycles.get(system_id)
            self.assertIsNotNone(cycle, "start dispatches a backend-owned cycle")
            await asyncio.wait_for(cycle, timeout=60)

        store = self.service.store
        tasks = {task["objective"]: task for task in store.owner_page("alice", system_id, "tasks", 0, 50)["items"]}
        self.assertIn("Draft the schema", tasks, "the lead's plan created real work")
        self.assertEqual(tasks["Draft the schema"]["state"], "done", "the lead accepted it after review")
        self.assertIn("users(id, email)", tasks["Draft the schema"]["result"])
        charged = store.db.execute("SELECT COALESCE(SUM(amount),0) FROM usage").fetchone()[0]
        self.assertGreater(charged, 0, "every provider call is charged to its attempt")
        self.assertEqual(store.db.execute(
            "SELECT COUNT(*) FROM attempts WHERE state NOT IN ('succeeded','cancelled')").fetchone()[0], 0)

    def autonomous_script(self, finish_after=1):
        """A lead that assigns one task per cycle and finishes after N cycles."""
        state = {"cycles": 0}

        def script(body):
            transcript = json.dumps(body["messages"])
            is_lead = "the lead of" in body["messages"][0]["content"]
            used_a_tool = any(message["role"] == "tool" for message in body["messages"])
            if not is_lead:
                return reply(calls=[("submit_result", {"summary": "Done", "output": "The work"})]) if used_a_tool \
                    else reply(calls=[("get_assigned_work", {})])
            if not used_a_tool:
                return reply(calls=[("get_assigned_work", {})])
            if "waiting on your review" in transcript:
                identifier = transcript.split("use review_work with these ids):")[1].split("- ")[1].split(" ")[0]
                return reply(calls=[("review_work", {"decisions": [
                    {"task": identifier, "verdict": "accept", "note": "Meets the objective"}]})])
            if "Review what this company has produced" in transcript:
                state["cycles"] += 1
                if state["cycles"] >= finish_after:
                    return reply(calls=[("finish_mission", {"summary": "Everything asked for is delivered.",
                                                            "delivered": "The tracker copy."})])
            return reply(calls=[("assign_plan", {"tasks": [
                {"key": f"k{state['cycles']}", "assignee": "Backend",
                 "objective": f"Do piece {state['cycles']}"}]})])

        return script

    async def run_autonomous(self, script, endpoint=None):
        self.server = StubServer(script)
        endpoint = endpoint or self.endpoint()
        payload = {**setup_payload(), "mode": "autonomous"}
        with patch.object(SwarmService, "_resolved", staticmethod(lambda endpoint_id: endpoint)), \
             patch.object(SwarmService, "_connection", staticmethod(lambda endpoint_id: endpoint)):
            created = await self.service.create("alice", payload, "create")
            system_id = created["id"]
            revision = self.service.store.get_system(system_id)["revision"]
            await self.service.lifecycle("alice", system_id, "start", "go", revision)
            cycle = self.service.cycles.get(system_id)
            if cycle:
                await asyncio.wait_for(cycle, timeout=120)
        return system_id

    async def test_an_autonomous_company_keeps_going_until_its_lead_finishes(self):
        """The requested behaviour: one idea, several cycles, a recorded end."""
        system_id = await self.run_autonomous(self.autonomous_script(finish_after=2))
        store = self.service.store
        self.assertEqual(store.run_count(system_id), 3, "two rounds of work plus the cycle that concluded")
        concluded = store.mission_concluded(system_id)
        self.assertEqual(concluded["reason"], "mission_complete")
        self.assertIn("delivered", concluded["summary"])
        self.assertEqual(store.get_system(system_id)["state"], "idle")
        done = store.db.execute("SELECT COUNT(*) FROM tasks WHERE system_id=? AND state='done'", (system_id,)).fetchone()[0]
        self.assertGreaterEqual(done, 4, "each cycle's plan, its work and its review were all accepted")

    async def test_a_lead_that_never_finishes_stops_at_the_ceiling(self):
        """Novelty alone must not keep a company spending."""
        system_id = await self.run_autonomous(self.autonomous_script(finish_after=99))
        store = self.service.store
        self.assertEqual(store.run_count(system_id), 5, "the cycle ceiling, not one more")
        self.assertEqual(store.mission_concluded(system_id)["reason"], "cycle_limit")
        self.assertEqual(self.service.cycles, {}, "the driver actually stopped")

    async def test_a_guided_company_still_runs_exactly_one_cycle(self):
        system_id = await self.run_autonomous(self.autonomous_script(finish_after=99))
        store = self.service.store
        self.server.close(); self.server = None
        # Same script, guided mode: the behaviour C1 shipped is unchanged.
        self.server = StubServer(self.autonomous_script(finish_after=99))
        endpoint = self.endpoint()
        with patch.object(SwarmService, "_resolved", staticmethod(lambda endpoint_id: endpoint)), \
             patch.object(SwarmService, "_connection", staticmethod(lambda endpoint_id: endpoint)):
            created = await self.service.create("alice", {**setup_payload(), "name": "Guided"}, "guided-create")
            guided = created["id"]
            revision = store.get_system(guided)["revision"]
            await self.service.lifecycle("alice", guided, "start", "go-guided", revision)
            cycle = self.service.cycles.get(guided)
            if cycle:
                await asyncio.wait_for(cycle, timeout=120)
        self.assertEqual(store.run_count(guided), 1)
        self.assertIsNone(store.mission_concluded(guided), "guided mode ends without concluding the mission")
        self.assertNotEqual(store.run_count(system_id), 1)

    async def test_a_team_that_needs_the_owner_says_so_and_resumes_when_answered(self):
        """David's first real run, reproduced.

        A specialist hit the no-file-access boundary and blocked, the lead
        escalated by blocking too, and the company wedged in silence with
        three tasks ready behind a dependency that would never finish.
        """
        state = {"blocked": True}

        def script(body):
            transcript = json.dumps(body["messages"])
            is_lead = "the lead of" in body["messages"][0]["content"]
            used_a_tool = any(message["role"] == "tool" for message in body["messages"])
            if not used_a_tool:
                return reply(calls=[("get_assigned_work", {})])
            if not is_lead:
                if state["blocked"]:
                    return reply(calls=[("report_blocker", {
                        "reason": "I have no file access and the objective needs a local file.",
                        "needs": "Paste the transcript text into a message."})])
                return reply(calls=[("submit_result", {"summary": "Done", "output": "The playbook"})])
            if "waiting on your review" in transcript:
                # The lead agrees it cannot be worked around and escalates.
                return reply(calls=[("report_blocker", {
                    "reason": "Nobody on the team can read that file.",
                    "needs": "Please paste the full transcript text here."})])
            if "Review what this company has produced" in transcript:
                return reply(calls=[("finish_mission", {"summary": "Delivered."})])
            return reply(calls=[("assign_plan", {"tasks": [
                {"key": "read", "assignee": "Backend", "objective": "Read the local transcript"}]})])

        system_id = await self.run_autonomous(script)
        store = self.service.store

        concluded = store.mission_concluded(system_id)
        self.assertIsNotNone(concluded, "a wedged company must say why it stopped, not go quiet")
        self.assertEqual(concluded["reason"], "needs_owner")
        self.assertIn("Paste the transcript", concluded["summary"])
        self.assertTrue(store.blocked_for_owner(system_id), "the request is still on the record")
        self.assertFalse(store.eligible_tasks(system_id), "nothing was claimable, which is why it stopped")

        # The owner answers. That alone should restart the work.
        state["blocked"] = False
        await self.service.message("alice", system_id, "Here is the transcript: ...", "answer")
        cycle = self.service.cycles.get(system_id)
        self.assertIsNotNone(cycle, "answering a waiting team resumes it without pressing Start")
        await asyncio.wait_for(cycle, timeout=120)

        self.assertIsNone(store.mission_concluded(system_id), "the mission reopened when it was answered")
        self.assertFalse(store.blocked_for_owner(system_id), "the held work was released")
        done = store.db.execute("SELECT COUNT(*) FROM tasks WHERE system_id=? AND state='done'", (system_id,)).fetchone()[0]
        self.assertGreater(done, 0, "work actually finished after the answer")

    async def test_a_lead_that_runs_out_of_turns_is_retried_then_conceded(self):
        """From David's run: the lead spent its turns messaging teammates and
        hit the ceiling. That used to raise, pause the whole company and leave
        an attempt needing reconciliation. It is a bounded outcome: retry once,
        then stop with a reason rather than wedging."""
        state = {"lead_steps": 0}

        def script(body):
            is_lead = "the lead of" in body["messages"][0]["content"]
            used_a_tool = any(message["role"] == "tool" for message in body["messages"])
            if not is_lead:
                return reply(calls=[("submit_result", {"summary": "s", "output": "o"})]) if used_a_tool \
                    else reply(calls=[("get_assigned_work", {})])
            if not used_a_tool:
                return reply(calls=[("get_assigned_work", {})])
            state["lead_steps"] += 1
            # Never assigns, never finishes - it just talks, like a lead that
            # burns its turns on messages.
            return reply(calls=[("send_message", {"to": "Backend", "body": "thinking out loud"})])

        system_id = await self.run_autonomous(script)
        store = self.service.store
        seeded = store.db.execute(
            "SELECT id FROM tasks WHERE system_id=? ORDER BY created_at LIMIT 1", (system_id,)).fetchone()["id"]
        self.assertGreaterEqual(store.attempt_count(seeded), 2, "the exhausted step was retried once")
        self.assertIsNotNone(store.mission_concluded(system_id), "and then the company said why it stopped")
        self.assertEqual(store.get_system(system_id)["state"], "active",
                         "an exhausted lead is not a fault, so nothing is paused")
        unknown = store.db.execute(
            "SELECT COUNT(*) FROM attempts WHERE system_id=? AND state='unknown'", (system_id,)).fetchone()[0]
        self.assertEqual(unknown, 0, "a bounded outcome must not leave work needing reconciliation")

    async def test_a_stopped_company_can_be_started_again(self):
        endpoint = {"id": "endpoint-a", "name": "Stub", "kind": "api",
                    "base_url": "http://127.0.0.1:1/v1", "model": "m", "api_key": None, "num_ctx": None}
        with patch.object(SwarmService, "_resolved", staticmethod(lambda endpoint_id: endpoint)), \
             patch.object(SwarmService, "_connection", staticmethod(lambda endpoint_id: endpoint)):
            created = await self.service.create("alice", setup_payload(), "create")
            system_id = created["id"]
            store = self.service.store
            revision = store.get_system(system_id)["revision"]
            await self.service.lifecycle("alice", system_id, "start", "one", revision)
            if self.service.cycles.get(system_id):
                await asyncio.wait_for(self.service.cycles[system_id], timeout=60)
            revision = store.get_system(system_id)["revision"]
            await self.service.lifecycle("alice", system_id, "stop", "stop", revision)
            self.assertEqual(store.get_system(system_id)["state"], "stopped")
            revision = store.get_system(system_id)["revision"]
            result = await self.service.lifecycle("alice", system_id, "start", "two", revision)
        self.assertEqual(result["status"], "active", result)
        self.assertEqual(store.run_count(system_id), 2, "a second run really opened")

    async def test_a_specialist_cannot_end_the_mission(self):
        root = tempfile.TemporaryDirectory(dir=environment.name)
        fixture = Fixture(root.name)
        try:
            service = fixture.service_for("Backend")
            service.bind(fixture.assignment("Backend", fixture.task("Backend")))
            self.assertNotIn("finish_mission", service.definitions)
            with self.assertRaises(ToolRejected):
                service.call("finish_mission", {"summary": "I decided we are done"})
        finally:
            fixture.close(); root.cleanup()

    async def test_a_team_without_connections_cannot_start(self):
        with patch.object(SwarmService, "_resolved", staticmethod(lambda endpoint_id: None)),              patch.object(SwarmService, "_connection", staticmethod(lambda endpoint_id: None)):
            payload = setup_payload(lead_endpoint=None, specialist_endpoint=None)
            created = await self.service.create("alice", payload, "create")
            revision = self.service.store.get_system(created["id"])["revision"]
            result = await self.service.lifecycle("alice", created["id"], "start", "go", revision)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["code"], "setup_blocked")
        self.assertIn("PM has no model connection", result["detail"])
        self.assertEqual(self.service.cycles, {})
        self.assertEqual(self.service.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    async def test_two_agents_on_one_account_share_one_pool(self):
        endpoint = {"id": "endpoint-a", "name": "Stub", "kind": "api",
                    "base_url": "https://api.example.com/v1", "model": "m", "api_key": "sk-1", "num_ctx": None}
        with patch.object(SwarmService, "_resolved", staticmethod(lambda endpoint_id: endpoint)),              patch.object(SwarmService, "_connection", staticmethod(lambda endpoint_id: endpoint)):
            created = await self.service.create("alice", setup_payload(), "create")
        pools = {agent["pool_id"] for agent in self.service.store.team(created["id"])}
        self.assertEqual(len(pools), 1, "one real account is one allowance")
        stored = self.service.store.db.execute("SELECT account_key FROM pools WHERE id=?", (pools.pop(),)).fetchone()[0]
        self.assertTrue(stored)
        self.assertNotIn("sk-1", stored)


if __name__ == "__main__":
    unittest.main(verbosity=2)
