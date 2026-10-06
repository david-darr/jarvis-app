"""The run contract (core/runs.py; spec: the vault note "Run Contracts -
Phase 1 (Build Spec)"). What these prove, on the same fakes the chat suite
uses (Claude SDK messages, a Codex process's JSON lines, a scripted
OpenAI-compatible endpoint) and real processes where stopping is the point:

- each brain's old text stream is exactly the text of its events, and its
  events are well formed: tool calls started then finished, usage, RESULT last;
- usage reads the same from every provider shape as before, and a turn's
  calls are summed: a local tool turn and a task's run both reach Home;
- a stop is reported confirmed only with evidence;
- Swarm's event kinds are the contract's.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
environment = tempfile.TemporaryDirectory(prefix="jarvis-runs-")
os.environ["JARVIS_DATA_DIR"] = environment.name

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402
from claude_agent_sdk.types import StreamEvent, ToolResultBlock, ToolUseBlock, UserMessage  # noqa: E402

from core import runs, task_scheduler, token_usage  # noqa: E402
from core.brain import Brain  # noqa: E402
from core.codex_brain import CodexBrain, _kill_process_tree  # noqa: E402
from core.external_brain import ExternalBrain  # noqa: E402
from core.providers import openai_compatible  # noqa: E402
from core.session_manager import session_manager  # noqa: E402
from core.swarm.models import EventKind as SwarmEventKind  # noqa: E402
from services import chat_service  # noqa: E402

LOCAL = {"id": "local", "name": "Local", "kind": "local", "model": "local-model"}
K = runs.EventKind


async def collect(stream):
    return [item async for item in stream]


def kinds(items):
    return [item.kind for item in items]


def joined(items):
    return "".join(item.data["text"] for item in items if item.kind is K.TEXT)


def assert_well_formed(test, items):
    """RESULT exactly once and last; every tool finished was started first."""
    test.assertEqual(kinds(items).count(K.RESULT), 1)
    test.assertIs(items[-1].kind, K.RESULT)
    started = set()
    for item in items:
        if item.kind is K.TOOL_STARTED:
            started.add(item.data["id"])
        elif item.kind is K.TOOL_FINISHED:
            test.assertIn(item.data["id"], started, "a tool finished that never started")


def usage_summary():
    return token_usage.get_usage_summary()


def reset_usage():
    if os.path.exists(token_usage.USAGE_FILE):
        os.remove(token_usage.USAGE_FILE)


# -- usage ------------------------------------------------------------------------

class UsageTests(unittest.TestCase):
    def test_every_shape_reads_as_before(self):
        """The context, cache and total numbers per provider shape, as
        core/token_usage.py read them before the contract existed."""
        table = [
            # Claude Code: input is only the uncached part.
            ({"input_tokens": 3, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 40, "output_tokens": 12},
             943, (900, 40, 3), 955),
            # Codex as its brain stores it: input includes the cached part.
            ({"total_tokens": 1100, "input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 100},
             1000, (800, None, 200), 1100),
            ({"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}, 50, (None, None, None), 60),
            ({"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105, "prompt_tokens_details": {"cached_tokens": 60}},
             100, (60, None, 40), 105),
            ({"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,
              "prompt_cache_hit_tokens": 70, "prompt_cache_miss_tokens": 30}, 100, (70, None, 30), 105),
            ({"input_tokens": 3, "output_tokens": 5}, 3, (None, None, None), 8),
            ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, None, (None, None, None), 0),
        ]
        for raw, context, (read, write, uncached), whole in table:
            with self.subTest(raw=raw):
                self.assertEqual(token_usage.extract_context_tokens(raw), context)
                self.assertEqual(token_usage.extract_cache_tokens(raw),
                                 {"cache_read_tokens": read, "cache_write_tokens": write, "uncached_input_tokens": uncached})
                self.assertEqual(token_usage._extract_total_tokens(raw), whole)
        self.assertIsNone(runs.normalize_usage(None))
        self.assertIsNone(runs.normalize_usage({}))

    def test_the_responses_api_cache_is_now_read(self):
        """The one shape that changed (2026-10-05): OpenAI's Responses API
        reports cached input under input_tokens_details, which was ignored."""
        raw = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
               "input_tokens_details": {"cached_tokens": 100}, "output_tokens_details": {"reasoning_tokens": 10}}
        usage = runs.normalize_usage(raw)
        self.assertEqual((usage.prompt_tokens, usage.cache_read_tokens, usage.uncached_input_tokens), (120, 100, 20))
        self.assertEqual(usage.reasoning_tokens, 10)

    def test_a_sum_keeps_unknowns_unknown(self):
        a = runs.normalize_usage({"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60})
        b = runs.normalize_usage({"prompt_tokens": 70, "completion_tokens": 5, "total_tokens": 75,
                                  "prompt_tokens_details": {"cached_tokens": 40}})
        both = runs.total([a, None, b])
        self.assertEqual((both.prompt_tokens, both.output_tokens, both.total_tokens, both.requests), (120, 15, 135, 2))
        self.assertEqual(both.cache_read_tokens, 40, "an unreported part adds nothing, not a guess")
        self.assertIsNone(runs.total([None]))


# -- the adapters -------------------------------------------------------------------

def stream_event(payload):
    return StreamEvent(uuid="t", session_id="sdk", event=payload)


CLAUDE_TURN = [
    stream_event({"type": "message_start"}),
    stream_event({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Looking "}}),
    stream_event({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "now."}}),
    AssistantMessage(content=[TextBlock(text="Looking now."), ToolUseBlock(id="tu1", name="Bash", input={"command": "ls"})],
                     model="test"),
    UserMessage(content=[ToolResultBlock(tool_use_id="tu1", content="a.txt", is_error=False)]),
    stream_event({"type": "message_start"}),
    AssistantMessage(content=[TextBlock(text="One file.")], model="test"),
    ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="cli-1",
                  usage={"input_tokens": 3, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 40,
                         "output_tokens": 12}),
]


class FakeSDK:
    def __init__(self, messages):
        self.messages = messages

    async def query(self, text):
        pass

    async def receive_response(self):
        for message in self.messages:
            yield message

    async def disconnect(self):
        pass


class ClaudeTests(unittest.IsolatedAsyncioTestCase):
    def brain(self):
        brain = Brain(vault_dir=environment.name)
        brain._client = FakeSDK(CLAUDE_TURN)
        return brain

    async def test_text_is_the_text_of_the_events(self):
        items = await collect(self.brain().events("x"))
        text = await collect(self.brain().run_turn_stream("x"))
        self.assertEqual(text, ["Looking ", "now.", "\n\n", "One file."])
        self.assertEqual("".join(text), joined(items))
        assert_well_formed(self, items)
        tools = [i for i in items if i.kind in (K.TOOL_STARTED, K.TOOL_FINISHED)]
        self.assertEqual([(i.kind, i.data["id"]) for i in tools], [(K.TOOL_STARTED, "tu1"), (K.TOOL_FINISHED, "tu1")])
        self.assertEqual(tools[0].data["name"], "Bash")
        self.assertEqual((tools[1].data["ok"], tools[1].data["output"]), (True, "a.txt"))
        usage = [i.data["usage"] for i in items if i.kind is K.USAGE]
        self.assertEqual([u.total_tokens for u in usage], [955])
        self.assertTrue(items[-1].data["usage_complete"])
        self.assertEqual(items[-1].provider_meta, {"cli_session_id": "cli-1"})

    async def test_its_stop_is_never_called_confirmed(self):
        stop = await self.brain().cancel()
        self.assertFalse(stop.confirmed)


class FakeProc:
    def __init__(self, lines):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data("".join(json.dumps(line) + "\n" for line in lines).encode())
        self.stdout.feed_eof()
        self.returncode = 0

    async def wait(self):
        return 0


CODEX_TURN = [
    {"type": "thread.started", "thread_id": "th-1"},
    {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "dir", "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "dir",
                                        "aggregated_output": "a.txt", "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "command": "del a.txt",
                                        "aggregated_output": "denied", "exit_code": 1, "status": "failed"}},
    {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "One file; deleting failed."}},
    {"type": "item.completed", "item": {"id": "i4", "type": "todo_list", "items": []}},
    {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 100}},
]


class CodexTests(unittest.IsolatedAsyncioTestCase):
    async def turn(self, lines):
        brain = CodexBrain(vault_dir=environment.name)
        stderr = asyncio.create_task(asyncio.sleep(0, result=b""))
        return brain, await collect(brain._consume_process(FakeProc(lines), stderr, True, "x"))

    async def test_tools_text_usage_and_result(self):
        brain, items = await self.turn(CODEX_TURN)
        assert_well_formed(self, items)
        self.assertEqual(joined(items), "One file; deleting failed.")
        finished = [i.data for i in items if i.kind is K.TOOL_FINISHED]
        self.assertEqual([(f["id"], f["ok"]) for f in finished], [("i1", True), ("i2", False)],
                         "a failed command is not ok, and a plan is not a tool")
        self.assertEqual(kinds(items).count(K.TOOL_STARTED), 2, "a tool seen only at completion still starts first")
        self.assertEqual([i.data["usage"].total_tokens for i in items if i.kind is K.USAGE], [1100])
        self.assertEqual(items[-1].provider_meta, {"thread_id": "th-1"})

    async def test_a_failed_turn_still_raises(self):
        with self.assertRaises(RuntimeError):
            await self.turn([{"type": "turn.failed", "error": {"message": "bad model"}}])

    async def test_a_stop_is_confirmed_only_when_the_tree_is_killed(self):
        sleeper = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
        self.assertTrue(await _kill_process_tree(sleeper))
        self.assertIsNotNone(sleeper.returncode)
        self.assertTrue(await _kill_process_tree(sleeper), "a process that already ended is stopped")

        brain = CodexBrain(vault_dir=environment.name)
        self.assertTrue((await brain.cancel()).confirmed, "nothing was running")
        brain._proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
        with patch("core.codex_brain.asyncio.create_subprocess_exec") as killer:
            async def refused(*args, **kwargs):
                class Failed:
                    async def wait(self):
                        return 128
                return Failed()
            killer.side_effect = refused
            stop = await brain.cancel()
        self.assertFalse(stop.confirmed, "only the parent was killed: the tree is not proven stopped")


class Scripted:
    """An OpenAI-compatible endpoint at the boundary every request crosses."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.bodies = []

    async def __call__(self, client, base_url, api_key, body):
        self.bodies.append(json.loads(json.dumps(body)))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def reply(message, usage=None):
    return {"choices": [{"message": message}], **({"usage": usage} if usage else {})}


def tool_call(call_id="c1", name="search_vault"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": '{"query": "falcon"}'}}]}


CALL_1 = {"prompt_tokens": 400, "completion_tokens": 20, "total_tokens": 420}
CALL_2 = {"prompt_tokens": 480, "completion_tokens": 30, "total_tokens": 510}


class ExternalTests(unittest.IsolatedAsyncioTestCase):
    def brain(self):
        brain = ExternalBrain("http://fake", "m", None, session_id=None)

        async def run_tool(name, args):
            self.ran.append(name)
            return "a note about falcons"
        brain._run_tool = run_tool
        return brain

    async def asyncSetUp(self):
        self.ran = []
        self.brain_ = self.brain()

    async def test_every_call_is_counted_and_the_text_is_the_events(self):
        endpoint = Scripted(reply(tool_call(), CALL_1), reply({"role": "assistant", "content": "Found it."}, CALL_2))
        with patch.object(openai_compatible, "_post_chat", new=endpoint):
            items = await collect(self.brain_.events("find falcons"))
        assert_well_formed(self, items)
        self.assertEqual(joined(items), "Found it.")
        self.assertEqual(self.ran, ["search_vault"])
        self.assertEqual([i.data["usage"].total_tokens for i in items if i.kind is K.USAGE], [420, 510])
        self.assertTrue(items[-1].data["usage_complete"])
        self.assertEqual(self.brain_.last_usage, CALL_2, "the context meter still reads the last call")
        self.assertEqual(self.brain_._messages[-1], {"role": "assistant", "content": "Found it."})

        endpoint = Scripted(reply(tool_call(), CALL_1), reply({"role": "assistant", "content": "Found it."}))
        with patch.object(openai_compatible, "_post_chat", new=endpoint):
            items = await collect(self.brain().events("find falcons"))
        self.assertFalse(items[-1].data["usage_complete"], "a call that reported nothing leaves usage incomplete")

    async def test_whole_reply_callers_make_plain_requests(self):
        """send_message and tasks read whole replies (stream=False): a server
        that rejects tools gets one plain request through the same boundary,
        as run_turn always sent, never a token stream."""
        request = openai_compatible.httpx.Request("POST", "http://fake")
        rejected = openai_compatible.httpx.HTTPStatusError(
            "bad", request=request, response=openai_compatible.httpx.Response(400, request=request))
        endpoint = Scripted(reply(tool_call()), rejected, reply({"role": "assistant", "content": "plain"}))
        with patch.object(openai_compatible, "_post_chat", new=endpoint):
            outcome = await runs.complete(self.brain(), "hi", runs.RunContext("task"))
        self.assertEqual(outcome.text, "plain")
        self.assertEqual((len(endpoint.bodies), outcome.tool_calls), (3, 1))
        self.assertNotIn("tools", endpoint.bodies[-1])
        self.assertFalse(any(m.get("role") == "tool" for m in endpoint.bodies[-1]["messages"]),
                         "the plain retry carries text only")

    async def test_a_stop_mid_tool_is_unconfirmed_and_the_history_stays_valid(self):
        running = asyncio.Event()

        async def slow_tool(name, args):
            running.set()
            await asyncio.sleep(30)
        self.brain_._run_tool = slow_tool
        endpoint = Scripted(reply({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "run_code", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "search_vault", "arguments": "{}"}}]}))
        with patch.object(openai_compatible, "_post_chat", new=endpoint):
            turn = asyncio.create_task(collect(self.brain_.events("go")))
            await asyncio.wait_for(running.wait(), 5)
            turn.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await turn
        stop = await self.brain_.cancel()
        self.assertFalse(stop.confirmed)
        self.assertIn("run_code", stop.how)
        answers = {m["tool_call_id"]: m["content"] for m in self.brain_.last_tool_rounds if m.get("role") == "tool"}
        self.assertTrue(answers["c1"].startswith("Stopped:"), "the running tool may or may not have finished")
        self.assertTrue(answers["c2"].startswith("Not run:"), "the next one never started")
        self.assertTrue((await self.brain().cancel()).confirmed, "with no tool cut off, nothing of JARVIS's runs on")


# -- usage reaches Home ----------------------------------------------------------------

class HomeUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_usage()
        chat_service._brains.clear()
        chat_service._busy.clear()
        self.sid = session_manager.create_session("runs")["id"]
        patch.object(chat_service, "_resolve_endpoint", return_value=LOCAL).start()
        patch("core.model_endpoints.resolve_runtime", return_value=("http://fake", "local-model", None, None)).start()
        patch.object(chat_service, "_run_context", side_effect=lambda *a: runs.RunContext("chat")).start()

    async def asyncTearDown(self):
        patch.stopall()
        chat_service._brains.clear()

    def script(self):
        return Scripted(reply(tool_call(), CALL_1), reply({"role": "assistant", "content": "Found it."}, CALL_2))

    async def test_a_tool_turn_counts_every_call(self):
        with patch.object(openai_compatible, "_post_chat", new=self.script()), \
             patch.object(ExternalBrain, "_run_tool", new=lambda self, name, args: asyncio.sleep(0, result="ok")):
            self.assertEqual(await chat_service.send_message(self.sid, "find falcons"), "Found it.")
        self.assertEqual(usage_summary()["local"]["total_tokens"], 930, "both calls, not only the last (510)")

        reset_usage()
        chat_service._brains.clear()
        with patch.object(openai_compatible, "_post_chat", new=self.script()), \
             patch.object(ExternalBrain, "_run_tool", new=lambda self, name, args: asyncio.sleep(0, result="ok")):
            text = "".join([c async for c in chat_service.stream_message(self.sid, "again") if isinstance(c, str)])
        self.assertEqual(text, "Found it.")
        self.assertEqual(usage_summary()["local"]["total_tokens"], 930, "the streamed chat counts both calls too")
        context = session_manager.get_session(self.sid)["context_state"]
        self.assertEqual(context["used_tokens"], 480, "the context meter is the last call's prompt")


class FakeAdapter:
    def __init__(self, *usages, text="the brief"):
        self.usages = usages
        self.text = text

    async def events(self, prompt, stream=True):
        yield runs.text(self.text)
        for usage in self.usages:
            yield runs.usage_event(usage)
        yield runs.result(True)


class TaskUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_usage()

    async def test_a_tasks_run_reaches_home(self):
        text = await task_scheduler.complete(FakeAdapter(CALL_1, CALL_2), "brief me", {"endpoint_id": "local"}, "card")
        self.assertEqual(text, "the brief")
        self.assertEqual(usage_summary()["local"]["total_tokens"], 930)

    async def test_a_task_on_the_default_claude_counts_only_when_unambiguous(self):
        one = [{"id": "claude", "kind": "claude_cli"}, {"id": "local", "kind": "local"}]
        with patch("core.model_endpoints.list_endpoints", return_value=one):
            await task_scheduler.complete(FakeAdapter(CALL_1), "x", {}, "task")
        self.assertEqual(usage_summary()["claude"]["total_tokens"], 420)
        reset_usage()
        two = one + [{"id": "claude-2", "kind": "claude_cli"}]
        with patch("core.model_endpoints.list_endpoints", return_value=two):
            await task_scheduler.complete(FakeAdapter(CALL_1), "x", {}, "task")
        self.assertEqual(usage_summary(), {}, "which Claude connection it was is unknown: nothing is guessed")

    async def test_a_run_without_a_result_is_an_error(self):
        class Unfinished:
            async def events(self, prompt, stream=True):
                yield runs.text("half")
        with self.assertRaises(RuntimeError):
            await runs.complete(Unfinished(), "x", runs.RunContext("task"))


# -- a stopped chat ------------------------------------------------------------------

class ChatStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_stop_goes_through_cancel(self):
        chat_service._brains.clear()
        chat_service._busy.clear()
        sid = session_manager.create_session("stop")["id"]
        stopped = []
        streaming = asyncio.Event()

        class Slow:
            is_admin = False

            async def events(self, prompt, stream=True):
                yield runs.text("partial")
                streaming.set()
                await asyncio.sleep(30)

            async def cancel(self):
                stopped.append(True)
                return runs.StopResult(True, "test")

            async def disconnect(self):
                pass

        async def consume():
            async for _ in chat_service.stream_message(sid, "go"):
                pass

        with patch.object(chat_service, "_resolve_endpoint", return_value=LOCAL), \
             patch.object(chat_service, "_get_brain", return_value=(Slow(), False)):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(streaming.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(stopped, [True])
        last = session_manager.get_session(sid)["messages"][-1]
        self.assertEqual((last["content"], last["status"]), ("partial", "interrupted"))


class SwarmTests(unittest.TestCase):
    def test_swarm_events_are_the_contracts(self):
        contract = {kind.value for kind in runs.EventKind}
        self.assertLessEqual({kind.value for kind in SwarmEventKind}, contract)


if __name__ == "__main__":
    unittest.main()
