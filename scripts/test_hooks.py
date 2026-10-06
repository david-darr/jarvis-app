"""Lifecycle hooks (services/hook_service.py, routes/hook_routes.py).

Spec: the vault note "Lifecycle Hooks (Build Spec)". What these prove, with
real commands run on this machine, a real chat turn through the tool loop,
and a local web server:

- a command hook blocks a tool by exit code 2 or by JSON, the tool does not
  run, and the model is told why; Claude's own tool hooks deny the same way;
- event data reaches a command only on stdin: never its command line, and
  JARVIS's private tokens are not in its environment;
- a failing or slow command lets the tool go ahead unless told otherwise,
  and a slow one is stopped;
- every event fires with its fields; filters, pause-all and no re-firing;
- the web-address, vault-note and channel kinds; admin-only, no tool.
"""
import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
environment = tempfile.TemporaryDirectory(prefix="jarvis-hooks-")
os.environ["JARVIS_DATA_DIR"] = environment.name

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import tool_registry
from core.brain import Brain
from core.middleware import require_admin
from core.session_manager import session_manager
from routes import hook_routes
from services import chat_service
from services.agent_service import agent_service
from services.hook_service import hook_service
from services.task_service import task_service

LOCAL = {"id": "local", "name": "Local", "kind": "local", "model": "local-model"}
SCRIPTS = Path(environment.name) / "scripts"
SCRIPTS.mkdir()


def script(name: str, body: str) -> str:
    path = SCRIPTS / f"{name}.py"
    path.write_text("import json, os, sys, time\n" + body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


RECORD = SCRIPTS / "record.json"
RECORDER = script("recorder", f"""
data = json.load(sys.stdin)
json.dump({{"stdin": data, "argv": sys.argv, "token": os.environ.get("JARVIS_TOOL_TOKEN"),
           "event_env": os.environ.get("JARVIS_HOOK_EVENT")}}, open(r"{RECORD}", "w"))
""")
BLOCK_EXIT = script("block_exit", 'json.load(sys.stdin)\nsys.stderr.write("no notes today")\nsys.exit(2)\n')
BLOCK_JSON = script("block_json", 'json.load(sys.stdin)\nprint(json.dumps({"decision": "block", "reason": "not that tool"}))\n')
FAILS = script("fails", "sys.exit(1)\n")
SLOW = script("slow", "time.sleep(30)\n")
PID_FILE = SCRIPTS / "slow.pid"
SLOW_PID = script("slow_pid", f'open(r"{PID_FILE}", "w").write(str(os.getpid()))\ntime.sleep(30)\n')


def alive(pid: int) -> bool:
    listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in listed


class Collector(BaseHTTPRequestHandler):
    received = []
    fail_first = False

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        Collector.received.append((dict(self.headers), body))
        status = 500 if Collector.fail_first and len(Collector.received) == 1 else 200
        self.send_response(status)
        self.end_headers()

    def log_message(self, *_):
        pass


class HookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for hook in hook_service.hooks():
            hook_service.delete(hook["id"])
        hook_service.set_paused(False)
        for agent in agent_service.list_agents():
            agent_service.delete(agent["id"])
        if RECORD.exists():
            RECORD.unlink()
        self.sent = AsyncMock(return_value=True)
        patch("core.channels.registry.send_to_channel", self.sent).start()
        app = FastAPI()
        app.include_router(hook_routes.router)
        app.dependency_overrides[require_admin] = lambda: "admin"
        self.client = TestClient(app)

    async def asyncTearDown(self):
        await hook_service.drain()
        patch.stopall()
        chat_service._brains.clear()

    def hook(self, **fields):
        body = {"name": "A hook", **fields}
        response = self.client.post("/api/hooks", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def log(self, hook):
        return [e["outcome"] for e in hook_service.get(hook["id"])["log"]]

    # -- blocking ---------------------------------------------------------------

    async def test_a_command_blocks_by_exit_code_or_by_json(self):
        self.hook(event="tool.before", action="command", tool_pattern="create_note", config={"command": BLOCK_EXIT})
        self.assertEqual(await hook_service.before_tool("create_note", {"text": "x"}, source="chat"), "no notes today")
        self.assertIsNone(await hook_service.before_tool("search_sessions", {"query": "x"}, source="chat"), "the pattern limits it")
        json_hook = self.hook(event="tool.before", action="command", tool_pattern="Bash|Write", config={"command": BLOCK_JSON})
        self.assertEqual(await hook_service.before_tool("Write", {}, source="agent"), "not that tool")
        self.assertEqual(self.log(json_hook), ["blocked"])

    async def test_a_block_reaches_the_model_and_the_tool_does_not_run(self):
        blocker = self.hook(event="tool.before", action="command", tool_pattern="create_note", config={"command": BLOCK_EXIT})
        self.hook(name="After", event="tool.after", action="channel", config={"channel_id": "conn:x", "template": "{tool} -> {result}"})
        requests = []

        async def model(client, base_url, api_key, body):
            requests.append(json.loads(json.dumps(body)))
            if len(requests) == 1:
                return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "create_note", "arguments": json.dumps({"text": "from the model"})}}]}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "Done."}}]}

        async def turn():
            requests.clear()
            chat_service._brains.clear()
            sid = session_manager.create_session("hooks")["id"]
            with patch.object(chat_service, "_resolve_endpoint", return_value=LOCAL), \
                 patch("core.model_endpoints.resolve_runtime", return_value=("http://fake", "local-model", None, None)), \
                 patch("core.providers.openai_compatible._post_chat", new=model):
                await chat_service.send_message(sid, "make a note", is_admin=True)
            await hook_service.drain()
            return next(m["content"] for m in requests[1]["messages"] if m.get("role") == "tool")

        self.assertEqual(await turn(), "Not run: blocked by a hook: no notes today")
        self.assertNotIn("from the model", {n["text"] for n in _notes()}, "the tool did not run")
        self.sent.assert_not_called()  # no "after" for a tool that never ran
        hook_service.delete(blocker["id"])
        self.assertNotIn("Not run", await turn())
        self.assertIn("from the model", {n["text"] for n in _notes()}, "without the hook it runs")
        self.assertTrue(self.sent.call_args_list[0].args[1].startswith("create_note -> "), "and the after-tool hook saw it")

    async def test_claude_tools_are_denied_through_the_sdk_hook(self):
        self.hook(event="tool.before", action="command", tool_pattern="Bash", config={"command": BLOCK_JSON})
        brain = Brain(vault_dir=environment.name, session_id=None, agent_id=None)
        options = brain._options() if hasattr(brain, "_options") else None
        denied = await brain._hook_before_tool({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}, "t1", None)
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("not that tool", denied["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(await brain._hook_before_tool({"tool_name": "Read", "tool_input": {}}, "t2", None), {})
        if options is not None:
            self.assertIn("PreToolUse", options.hooks)

    # -- the command's boundary ---------------------------------------------------

    async def test_event_data_reaches_a_command_only_on_stdin(self):
        self.hook(event="tool.before", action="command", config={"command": RECORDER})
        hostile = {"command": '"; rm -rf ~ & echo pwned $(whoami)'}
        with patch.dict(os.environ, {"JARVIS_TOOL_TOKEN": "internal-secret"}):
            self.assertIsNone(await hook_service.before_tool("Bash", hostile, source="agent", agent_id="a1"))
        seen = json.loads(RECORD.read_text())
        self.assertEqual(seen["argv"][1:], [], "nothing but the script itself on the command line")
        self.assertIsNone(seen["token"], "JARVIS's internal token is not handed over")
        self.assertEqual(seen["event_env"], "tool.before")
        self.assertEqual(seen["stdin"]["input"], hostile)
        self.assertEqual(seen["stdin"]["tool"], "Bash")

    async def test_a_failing_or_slow_command_lets_the_tool_go_unless_told(self):
        failing = self.hook(event="tool.before", action="command", config={"command": FAILS})
        self.assertIsNone(await hook_service.before_tool("Bash", {}, source="chat"))
        self.assertEqual(self.log(failing), ["failed"])
        hook_service.update(failing["id"], config={"command": FAILS, "block_on_failure": True})
        self.assertIn("failed", await hook_service.before_tool("Bash", {}, source="chat"))
        hook_service.delete(failing["id"])
        slow = self.hook(event="tool.before", action="command", config={"command": SLOW, "timeout": 1})
        started = time.monotonic()
        self.assertIsNone(await hook_service.before_tool("Bash", {}, source="chat"))
        self.assertLess(time.monotonic() - started, 10, "a slow hook is stopped at its timeout")
        self.assertEqual(self.log(slow), ["timeout"])

    async def test_a_stopped_turn_stops_its_command(self):
        """A chat's Stop while a before-tool command runs kills the command
        too, rather than leaving it running with nobody waiting (2026-10-05)."""
        hook = self.hook(event="tool.before", action="command", config={"command": SLOW_PID})
        if PID_FILE.exists():
            PID_FILE.unlink()
        check = asyncio.create_task(hook_service.before_tool("Bash", {}, source="chat"))
        for _ in range(100):
            if PID_FILE.exists() and PID_FILE.read_text():
                break
            await asyncio.sleep(0.1)
        pid = int(PID_FILE.read_text())
        try:
            check.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await check
            for _ in range(50):
                if not alive(pid):
                    break
                await asyncio.sleep(0.1)
            self.assertFalse(alive(pid), "the command is killed with the turn")
            self.assertEqual(self.log(hook), ["skipped"])
        finally:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)

    # -- events, filters, pause, re-firing ------------------------------------------

    async def test_every_event_fires_with_its_fields(self):
        events = ["chat.reply", "task.finished", "card.review", "agent.inbox", "trigger.event"]
        for event in events:
            self.hook(name=event, event=event, action="channel", config={"channel_id": "conn:x", "template": "{event}|{summary}"})
        bo = agent_service.create("Bo")
        goal = task_service.create_task("Check", "Look.", "daily", run_time="09:00", agent_id=bo["id"])
        task_service.record_run(goal["id"], output="All quiet.")
        card = task_service.create_task("Card", "Do it.", "card", status="ready")
        task_service.finish_card(card["id"], "The result.")
        agent_service.notify(agent_service.add_item(bo["id"], "report", "A report", "Something turned up."))
        from services.trigger_service import trigger_service
        trigger, _ = trigger_service.create("Pushes")
        trigger_service.receive(trigger["id"], {}, b"{}")  # unsigned: rejected, and that is an event too

        async def model(client, base_url, api_key, body):
            return {"choices": [{"message": {"role": "assistant", "content": "Hi there."}}]}

        sid = session_manager.create_session("events")["id"]
        with patch.object(chat_service, "_resolve_endpoint", return_value=LOCAL), \
             patch("core.model_endpoints.resolve_runtime", return_value=("http://fake", "local-model", None, None)), \
             patch("core.providers.openai_compatible._post_chat", new=model):
            await chat_service.send_message(sid, "hello")
        await hook_service.drain()
        texts = sorted(call.args[1] for call in self.sent.call_args_list)
        self.assertEqual(texts, sorted(["chat.reply|Hi there.", "task.finished|All quiet.", "card.review|The result.",
                                        "agent.inbox|Something turned up.", "trigger.event|rejected"]))

    async def test_filters_pause_and_no_refiring(self):
        bo = agent_service.create("Bo")
        self.hook(event="task.finished", action="channel", agent_id=bo["id"], config={"channel_id": "conn:x", "template": "{task}"})
        self.hook(name="Tasks only", event="task.finished", action="channel", source="task", config={"channel_id": "conn:y", "template": "{task}"})
        hook_service.emit("task.finished", {"source": "agent", "agent_id": "someone-else", "task": "Theirs"})
        hook_service.emit("task.finished", {"source": "agent", "agent_id": bo["id"], "task": "Bo's"})
        hook_service.emit("task.finished", {"source": "task", "task": "Plain"})
        await hook_service.drain()
        self.assertEqual(sorted(c.args[1] for c in self.sent.call_args_list), ["Bo's", "Plain"])
        self.sent.reset_mock()
        self.client.post("/api/hooks/pause", json={"paused": True})
        hook_service.emit("task.finished", {"source": "task", "task": "Paused"})
        await hook_service.drain()
        self.sent.assert_not_called()
        self.client.post("/api/hooks/pause", json={"paused": False})

        async def sends_and_fires(channel, text):  # what a hook's action causes fires nothing
            hook_service.emit("task.finished", {"source": "task", "task": "Again"})
            return True
        self.sent.side_effect = sends_and_fires
        hook_service.emit("task.finished", {"source": "task", "task": "Once"})
        # Without the guard this never settles: each send fires another.
        await asyncio.wait_for(hook_service.drain(), timeout=5)
        self.assertEqual([c.args[1] for c in self.sent.call_args_list], ["Once"])

    # -- the other kinds -------------------------------------------------------------

    async def test_a_web_address_gets_signed_json_with_a_retry(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        Collector.received, Collector.fail_first = [], True
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/hook"
            self.hook(event="card.review", action="webhook", config={"url": url, "secret": "shh"})
            hook_service.emit("card.review", {"source": "task", "card": "Report", "output": "Ready."})
            await hook_service.drain()
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(len(Collector.received), 2, "the failed first attempt is retried once")
        headers, body = Collector.received[1]
        self.assertEqual(headers["X-JARVIS-Signature"], "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest())
        self.assertEqual(json.loads(body)["card"], "Report")
        listed = self.client.get("/api/hooks").json()["hooks"][0]
        self.assertIs(listed["config"]["secret"], True, "the secret is never listed")

    async def test_a_vault_note_gets_a_line_and_stays_in_the_vault(self):
        vault = Path(environment.name) / "vault"
        vault.mkdir(exist_ok=True)
        with patch("core.vault.resolve_vault_dir", return_value=str(vault)):
            self.hook(event="agent.inbox", action="vault_note", config={"path": "Logs/Agents", "template": "- {agent}: {text}"})
            for text in ("first", "second"):
                hook_service.emit("agent.inbox", {"source": "agent", "agent": "Bo", "text": text})
                await hook_service.drain()
        self.assertEqual((vault / "Logs" / "Agents.md").read_text(encoding="utf-8"), "- Bo: first\n- Bo: second")
        for bad in ("../outside", "/etc/passwd", "C:/Windows/x"):
            response = self.client.post("/api/hooks", json={"name": "x", "event": "agent.inbox", "action": "vault_note", "config": {"path": bad}})
            self.assertEqual(response.status_code, 400, bad)

    async def test_a_test_event_runs_the_hook(self):
        hook = self.hook(event="tool.before", action="command", config={"command": BLOCK_JSON})
        result = self.client.post(f"/api/hooks/{hook['id']}/test").json()
        self.assertEqual(result["blocked"], "not that tool")

    # -- who can touch hooks ---------------------------------------------------------------

    async def test_only_an_admin_and_no_model_tool(self):
        for route in hook_routes.router.routes:
            self.assertTrue(any(dep.call is require_admin for dep in route.dependant.dependencies), route.path)
        names = [spec.name for spec in tool_registry._REGISTRY.values()]
        self.assertFalse([n for n in names if "hook" in n.lower()], "no tool reaches hooks")

    async def test_bad_hooks_are_refused(self):
        bad = [{"event": "nope", "action": "channel"}, {"event": "chat.reply", "action": "nope"},
               {"event": "chat.reply", "action": "webhook", "config": {"url": "ftp://x"}},
               {"event": "chat.reply", "action": "command", "config": {"command": ""}},
               {"event": "chat.reply", "action": "channel", "config": {}}]
        for body in bad:
            self.assertEqual(self.client.post("/api/hooks", json={"name": "x", **body}).status_code, 400, body)


def _notes():
    from services.notes_service import notes_service
    return notes_service.list_notes()


def tearDownModule():
    # Release the session store so the temp data folder can be removed;
    # Windows will not delete a database that is still open.
    from core import session_manager_store
    session_manager_store.close()


if __name__ == "__main__":
    unittest.main()
