"""Codex bridge tests. Default: local fixtures only, no provider calls.

--cli exercises the installed Codex binary against a loopback fake Responses
provider, including its actual advertised tools and real MCP subprocess.
--live-luna spends real Codex login usage on one disposable guided company.
"""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_swarm_workers import Fixture, environment, setup_payload
from core.swarm.adapters import WorkerContext
from core.swarm.adapters.codex_worker import CodexWorker, config_args, codex_path, worker_environment
from core.swarm.models import Assignment, Conflict, EventKind
from core.swarm.runtime import SwarmRuntime
from core.swarm.worker_bridge import WorkerBridge
from services.swarm_service import SwarmService

RUN_CLI = "--cli" in sys.argv
if RUN_CLI:
    sys.argv.remove("--cli")
RUN_LIVE = "--live-luna" in sys.argv
if RUN_LIVE:
    sys.argv.remove("--live-luna")


def context(fixture, root, name="Backend"):
    return WorkerContext(endpoint={"kind": "codex_cli"}, agent=fixture.team[name],
                         system=fixture.store.get_system(fixture.system_id),
                         tool_service=fixture.service_for(name), scratch_dir=str(root),
                         model="gpt-5.6-luna", effort="low")


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory(dir=environment.name)
        self.fixture = Fixture(self.root.name)
        self.runtime = await SwarmRuntime(self.fixture.store, Path(self.root.name) / "handoffs").start()
        task_id = self.fixture.task("Backend")
        store = self.fixture.store
        attempt_id = store.reserve(task_id, self.runtime.runtime_id, self.runtime.generation, 5000)
        store.begin(attempt_id, self.runtime.runtime_id, self.runtime.generation)
        task = store.get_task(task_id)
        self.assignment = Assignment(attempt_id, task["system_id"], task["run_id"], task["agent_id"], task_id,
                                     task["objective"], 5000, {})
        self.queue = asyncio.Queue()
        self.service = self.fixture.service_for("Backend").bind(self.assignment)
        self.bridge = await WorkerBridge(self.service, self.queue).start()

    async def asyncTearDown(self):
        await self.bridge.close()
        await self.runtime.close()
        self.fixture.close()
        self.root.cleanup()

    async def request(self, **fields):
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.bridge.port), 5)
        writer.write(json.dumps(fields).encode() + b"\n")
        await writer.drain()
        answer = json.loads(await asyncio.wait_for(reader.readline(), 5))
        writer.close()
        await writer.wait_closed()
        return answer

    async def test_authentication_and_role_scoping(self):
        for token in (None, "wrong"):
            self.assertFalse((await self.request(token=token, method="list"))["ok"])
        result = await self.request(token=self.bridge.token, method="list")
        names = {item["name"] for item in result["result"]}
        self.assertIn("submit_result", names)
        self.assertNotIn("assign_plan", names)
        self.assertFalse((await self.request(token=self.bridge.token, method="call", id="x",
                                            name="assign_plan", arguments={})) ["ok"])

    async def test_concurrent_replay_runs_once_and_changed_replay_refuses(self):
        data = dict(token=self.bridge.token, method="call", id="same", name="record_finding",
                    arguments={"summary": "one finding"})
        first = asyncio.create_task(self.request(**data))
        second = asyncio.create_task(self.request(**data))
        key, name, arguments, future = await asyncio.wait_for(self.queue.get(), 2)
        future.set_result({"ok": True, "text": "recorded"})
        self.assertTrue((await first)["ok"])
        self.assertEqual(await second, await first)
        self.assertTrue(self.queue.empty())
        self.assertFalse((await self.request(**{**data, "arguments": {"summary": "changed"}}))["ok"])

    async def test_pause_and_expiry_revoke_reads_and_writes(self):
        self.fixture.store.pause(self.assignment.system_id)
        self.assertFalse((await self.request(token=self.bridge.token, method="list"))["ok"])
        with self.assertRaises(Conflict):
            self.bridge.validate()

    async def test_expired_attempt_refuses(self):
        self.fixture.store.db.execute("UPDATE attempts SET lease_until=0 WHERE id=?", (self.assignment.attempt_id,))
        self.assertFalse((await self.request(token=self.bridge.token, method="list"))["ok"])

    async def test_forged_identity_and_role_refuse(self):
        self.service.assignment = replace(self.assignment, agent_id=self.fixture.team["PM"]["id"])
        self.assertFalse((await self.request(token=self.bridge.token, method="list"))["ok"])
        self.service.assignment = self.assignment
        self.service.is_lead = True
        self.assertFalse((await self.request(token=self.bridge.token, method="list"))["ok"])

    async def test_pause_between_intent_and_execution_prevents_the_action(self):
        worker = CodexWorker(context(self.fixture, self.root.name))
        with patch("core.swarm.adapters.codex_worker.installation_blocker", return_value=None), \
             patch.object(worker, "_args", return_value=[sys.executable, "-I", "-c", "import time; time.sleep(90)"]):
            events = worker.events(self.assignment)
            next_event = asyncio.create_task(anext(events))
            async with asyncio.timeout(5):
                while not worker.bridge or not worker.bridge.port:
                    await asyncio.sleep(0.01)
            request = asyncio.create_task(worker.bridge._request({"token": worker.bridge.token, "method": "call",
                          "id": "pause-race", "name": "record_finding", "arguments": {"summary": "must not run"}}))
            event = await asyncio.wait_for(next_event, 5)
            self.assertEqual(event.kind, EventKind.ACTION_STARTED)
            self.fixture.store.pause(self.fixture.system_id)
            with patch.object(worker.context.tool_service, "call") as call:
                with self.assertRaises(Conflict):
                    await anext(events)
                call.assert_not_called()
            self.assertFalse((await request)["ok"])
            self.assertTrue(worker.stopped)

    async def test_close_unblocks_pending_and_invalidates_capability(self):
        waiting = asyncio.create_task(self.bridge._request({"token": self.bridge.token, "method": "call",
                    "id": "waiting", "name": "get_assigned_work", "arguments": {}}))
        await self.queue.get()
        await self.bridge.close()
        self.assertFalse((await waiting)["ok"])
        with self.assertRaises(Conflict):
            self.bridge.validate()

    def test_environment_excludes_app_and_provider_secrets(self):
        with patch.dict(os.environ, {"JARVIS_INTERNAL_TOKEN": "secret", "OPENAI_API_KEY": "secret",
                                     "AWS_SECRET_ACCESS_KEY": "secret", "JARVIS_CODEX_SESSION_ID": "secret"}):
            self.assertNotIn("secret", json.dumps(worker_environment()))


def process_running(pid):
    if sys.platform == "win32":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory(dir=environment.name)
        self.fixture = Fixture(self.root.name)
        self.runtime = await SwarmRuntime(self.fixture.store, Path(self.root.name) / "handoffs",
                                          poll_interval=0.02, stop_timeout=12).start()
        self.installation = patch("core.swarm.adapters.codex_worker.installation_blocker", return_value=None)
        self.installation.start()
        self.worker = None

    async def asyncTearDown(self):
        if self.worker:
            await self.worker.cancel()
        await self.runtime.close()
        self.fixture.close()
        self.installation.stop()
        self.root.cleanup()

    def make_worker(self, program):
        class ScriptWorker(CodexWorker):
            def _args(self, *_args):
                return [sys.executable, "-I", "-c", program]
        self.worker = ScriptWorker(context(self.fixture, self.root.name))
        return self.worker

    def tree_program(self, *, linger):
        pidfile = Path(self.root.name) / "child.pid"
        return pidfile, ("import subprocess,sys,time,pathlib,json; "
                        "child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(90)'],"
                        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                        f"pathlib.Path({str(pidfile)!r}).write_text(str(child.pid)); "
                        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':10}}),flush=True); "
                        + ("time.sleep(90)" if linger else "sys.exit(0)"))

    async def test_parent_exit_still_terminates_descendant(self):
        pidfile, program = self.tree_program(linger=False)
        worker = self.make_worker(program)
        attempt_id = await self.runtime.run_task(self.fixture.task("Backend"), worker, max_units=5000)
        self.assertTrue(worker.stopped)
        self.assertFalse(process_running(int(pidfile.read_text())))
        attempt = self.fixture.store.get_attempt(attempt_id)
        self.assertTrue(attempt["usage_complete"])
        result = json.loads(self.fixture.store.get_task(attempt["task_id"])["result"])
        self.assertEqual(result["status"], "incomplete", "prose/exit is not a submitted task")

    async def test_pause_revokes_access_and_stops_entire_tree(self):
        pidfile, program = self.tree_program(linger=True)
        worker = self.make_worker(program)
        runner = asyncio.create_task(self.runtime.run_task(self.fixture.task("Backend"), worker, max_units=5000))
        async with asyncio.timeout(10):
            while not pidfile.exists():
                await asyncio.sleep(0.02)
        await self.runtime.pause(self.fixture.system_id)
        attempt = self.fixture.store.get_attempt(await runner)
        self.assertTrue(worker.bridge.revoked)
        self.assertTrue(worker.stopped)
        self.assertFalse(process_running(int(pidfile.read_text())))
        self.assertEqual(attempt["state"], "cancelled")
        self.assertFalse(attempt["usage_complete"])
        self.assertGreater(attempt["held"], 0, "interrupted usage stays conservatively reserved")

    async def test_failure_drains_stderr_and_keeps_unknown_usage(self):
        worker = self.make_worker("import sys; sys.stderr.write('x'*100000+'fixture-failure'); sys.exit(4)")
        task = self.fixture.task("Backend")
        with self.assertRaisesRegex(RuntimeError, "exit 4.*fixture-failure"):
            await self.runtime.run_task(task, worker, max_units=5000)
        self.assertTrue(worker.stopped)
        self.assertLessEqual(len(worker.stderr), 8000)
        attempt = dict(self.fixture.store.db.execute("SELECT * FROM attempts WHERE task_id=?", (task,)).fetchone())
        self.assertFalse(attempt["usage_complete"])
        self.assertGreater(attempt["held"], 0)

    async def test_idle_timeout_stops_worker_without_fabricating_usage(self):
        worker = self.make_worker("import time; time.sleep(90)")
        with patch("core.swarm.adapters.codex_worker.MESSAGE_TIMEOUT_SECONDS", 0.2):
            attempt_id = await self.runtime.run_task(self.fixture.task("Backend"), worker, max_units=5000)
        attempt = self.fixture.store.get_attempt(attempt_id)
        self.assertTrue(worker.stopped)
        self.assertFalse(attempt["usage_complete"])
        self.assertGreater(attempt["held"], 0)
        self.assertIn("timed out", self.fixture.store.get_task(attempt["task_id"])["result"])

    @unittest.skipUnless(sys.platform == "win32", "Windows job attachment")
    async def test_attachment_failure_stops_waiting_supervisor(self):
        worker = self.make_worker("raise AssertionError('must never launch')")
        with patch("core.swarm.adapters.codex_worker._WindowsJob.attach", side_effect=OSError("fixture attach failure")):
            with self.assertRaisesRegex(OSError, "attach failure"):
                await self.runtime.run_task(self.fixture.task("Backend"), worker, max_units=5000)
        self.assertTrue(worker.stopped)
        self.assertIsNotNone(worker.process.returncode)

    async def test_cancel_during_os_spawn_waits_for_process_ownership(self):
        worker = self.make_worker("raise AssertionError('must never launch')")
        original = asyncio.create_subprocess_exec
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            process = await original(*args, **kwargs)
            # A venv launcher would have spawned its interpreter by now.
            await asyncio.sleep(0.2)
            entered.set()
            await release.wait()
            return process
        with patch("asyncio.create_subprocess_exec", delayed):
            runner = asyncio.create_task(self.runtime.run_task(self.fixture.task("Backend"), worker, max_units=5000))
            await asyncio.wait_for(entered.wait(), 5)
            stopping = asyncio.create_task(worker.cancel())
            await asyncio.sleep(0)
            self.assertFalse(stopping.done())
            release.set()
            self.assertTrue(await asyncio.wait_for(stopping, 5))
            await asyncio.gather(runner, return_exceptions=True)
        self.assertIsNotNone(worker.process.returncode)


class ResponsesFixture:
    def __init__(self):
        self.requests = []
        self.names = set()
        self.discovered = []
        self.errors = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    raw = self.rfile.read(int(self.headers["Content-Length"]))
                    body = json.loads(raw)
                    fixture.requests.append(body)
                    index = len(fixture.requests)
                    def collect(tools, namespace=""):
                        for tool in tools:
                            if tool.get("type") == "namespace":
                                collect(tool.get("tools", []), tool["name"] + ".")
                            else:
                                fixture.names.add(namespace + (tool.get("name") or tool.get("type")))
                    collect(body.get("tools", []))
                    for message in body.get("input", []):
                        if message.get("type") == "additional_tools":
                            collect(message.get("tools", []))
                    if index == 1:
                        item = {"id": "ct_1", "type": "custom_tool_call", "call_id": "call_1", "name": "exec",
                                "namespace": "functions", "input": "text(ALL_TOOLS.map(t => t.name)); text(await tools.mcp__swarm__get_assigned_work({}));", "status": "completed"}
                    elif index == 2:
                        for message in body.get("input", []):
                            if message.get("type") == "custom_tool_call_output":
                                for content in message.get("output", []):
                                    text = content.get("text", "")
                                    if text.startswith('["'):
                                        fixture.discovered = json.loads(text)
                        item = {"id": "ct_2", "type": "custom_tool_call", "call_id": "call_2", "name": "exec",
                                "namespace": "functions", "input": 'text(await tools.mcp__swarm__submit_result({summary:"Done",output:"A verified fixture result."}));',
                                "status": "completed"}
                    else:
                        item = {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": "Submitted.", "annotations": []}]}
                    response = {"id": f"resp_{index}", "object": "response", "created_at": 1, "status": "completed",
                                "model": "gpt-5.6-luna", "output": [item],
                                "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                                          "input_tokens_details": {"cached_tokens": 0}}}
                    events = [
                        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                        {"type": "response.output_item.added", "output_index": 0, "item": item},
                        {"type": "response.output_item.done", "output_index": 0, "item": item},
                        {"type": "response.completed", "response": response},
                    ]
                    encoded = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                                      for event in events).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except Exception as exc:
                    fixture.errors.append(repr(exc))
                    self.send_error(500)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@unittest.skipUnless(RUN_CLI, "--cli enables the installed-binary fixture")
class InstalledCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_cli_mcp_tools_results_and_usage(self):
        await self.run_cli_fixture("gpt-5.6-luna")

    async def test_sol_uses_the_same_restricted_tools(self):
        await self.run_cli_fixture("gpt-5.6-sol")

    async def run_cli_fixture(self, model):
        root = tempfile.TemporaryDirectory(dir=environment.name)
        fixture = Fixture(root.name)
        server = ResponsesFixture()
        runtime = await SwarmRuntime(fixture.store, Path(root.name) / "handoffs").start()
        codex_home = Path(root.name) / "codex-home"
        codex_home.mkdir()
        sentinel = Path(root.name) / "must-not-exist"
        (codex_home / "config.toml").write_text(
            'developer_instructions = "SWARM_INHERITED_RULE_CANARY"\n'
            '[features]\nshell_tool = true\n'
            '[mcp_servers.evil]\ncommand = ' + json.dumps(sys.executable) + '\nargs = ' +
            json.dumps(["-c", f"from pathlib import Path; Path({str(sentinel)!r}).touch()"]) + '\n', encoding="utf-8")

        class LocalWorker(CodexWorker):
            def _args(self, codex, scratch, assignment):
                args = super()._args(codex, scratch, assignment)
                return args[:-1] + config_args({
                    "model_provider": "swarm_fixture", "features.enable_request_compression": False,
                    "model_providers.swarm_fixture.name": "Swarm fixture",
                    "model_providers.swarm_fixture.base_url": server.url,
                    "model_providers.swarm_fixture.wire_api": "responses",
                    "model_providers.swarm_fixture.requires_openai_auth": False,
                    "model_providers.swarm_fixture.request_max_retries": 0,
                    "model_providers.swarm_fixture.stream_max_retries": 0,
                }) + ["-"]

        worker = LocalWorker(context(fixture, root.name))
        worker.context.model = model
        worker.context.env = {"CODEX_HOME": str(codex_home)}
        try:
            attempt_id = await asyncio.wait_for(runtime.run_task(fixture.task("Backend"), worker, max_units=5000), 50)
            attempt = fixture.store.get_attempt(attempt_id)
            self.assertEqual(attempt["state"], "succeeded")
            self.assertTrue(attempt["usage_complete"])
            self.assertEqual(fixture.store.get_task(attempt["task_id"])["state"], "review")
            self.assertEqual(json.loads(fixture.store.get_task(attempt["task_id"])["result"])["status"], "submitted")
            self.assertTrue(worker.stopped)
            self.assertGreaterEqual(len(server.requests), 3)
            print("Actual Codex tools:", sorted(server.names))
            self.assertTrue(server.discovered)
            expected = {"mcp__swarm__" + name for name in worker.context.tool_service.definitions}
            self.assertTrue(expected.issubset(set(server.discovered)))
            forbidden = set(server.discovered) - expected - {"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"}
            self.assertFalse(forbidden, f"Unscoped tools exposed: {forbidden}")
            print("Actual nested tools:", sorted(server.discovered))
            self.assertEqual(attempt["used"], 330)
            self.assertEqual(attempt["held"], 0)
            self.assertFalse(sentinel.exists(), "personal MCP configuration must not launch")
            self.assertNotIn("SWARM_INHERITED_RULE_CANARY", json.dumps(server.requests))
        finally:
            self.assertFalse(server.errors)
            await worker.cancel()
            await runtime.close()
            fixture.close()
            server.close()
            root.cleanup()


async def live_luna():
    """Explicit opt-in only; all company data is isolated from the real app."""
    root = Path(__file__).resolve().parents[1] / "data" / "swarm-codex-review" / ("live-" + uuid.uuid4().hex[:10])
    root.mkdir(parents=True)
    service = SwarmService(root)
    await service.start()
    endpoint = {"id": "live-codex", "name": "Codex login", "kind": "codex_cli",
                "model": "gpt-5.6-luna", "base_url": "", "api_key": None, "num_ctx": None}
    payload = setup_payload("live-codex", "live-codex")
    payload.update(name="Disposable Luna bridge check", mission="Produce exactly three short names for a fictional study timer. Each name needs one sentence explaining it. No external research is needed.")
    limit = {"ceiling": 200000, "pause_percent": 80, "checkpoint_reserve": 0}
    payload.update(system_limit=limit, run_limit=limit, pool_limit=limit)
    for agent in [payload["lead"], *payload["specialists"]]:
        agent.update(model="gpt-5.6-luna", effort="low", limit=limit)
        agent["instructions"] = "Keep this test brief. Read the assignment, perform your one required terminal tool action, and end your turn immediately."
    payload["specialists"][0].update(name="Writer", role="Writer")
    payload["lead"]["instructions"] += " Assign exactly one task to Writer. In the review step accept the work if it contains three names with explanations. Do not assign any additional tasks."
    system_id = None
    try:
        with patch.object(SwarmService, "_resolved", return_value=endpoint), \
             patch.object(SwarmService, "_connection", return_value=endpoint), \
             patch("core.swarm.adapters.codex_worker.STEP_TIMEOUT_SECONDS", 90), \
             patch("core.swarm.adapters.codex_worker.MAX_TURNS", 3), \
             patch("core.swarm.adapters.codex_worker.LEAD_MAX_TURNS", 3):
            system_id = (await service.create("bridge-test", payload, "create"))["id"]
            revision = service.store.get_system(system_id)["revision"]
            await service.lifecycle("bridge-test", system_id, "start", "start", revision,
                                    spend_confirmed=True)
            print("Live Luna company started; evidence:", root, flush=True)
            await asyncio.wait_for(service.cycles[system_id], 300)
        snapshot = service.store.owner_snapshot("bridge-test", system_id)
        attempts = [dict(row) for row in service.store.db.execute("SELECT * FROM attempts")]
        tasks = [dict(row) for row in service.store.db.execute("SELECT * FROM tasks")]
        output = {"system": snapshot["system"], "attempts": attempts, "tasks": tasks,
                  "used": sum(row["used"] for row in attempts), "held": sum(row["held"] for row in attempts)}
        (root / "result.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
        print(json.dumps({"states": [row["state"] for row in attempts], "used": output["used"],
                          "held": output["held"], "tasks": [row["state"] for row in tasks]}), flush=True)
        assert len(attempts) == 3, "Expected plan, specialist, and review attempts"
        assert all(row["state"] == "succeeded" and row["usage_complete"] and row["stopped"] for row in attempts)
        assert all(row["state"] == "done" for row in tasks)
        assert output["used"] > 0 and output["held"] == 0
        print("PASS: live Luna planned, submitted, and accepted the result with settled usage and confirmed stops.", flush=True)
    finally:
        await service.close()


if __name__ == "__main__":
    if RUN_LIVE:
        asyncio.run(live_luna())
    else:
        unittest.main(verbosity=2)
