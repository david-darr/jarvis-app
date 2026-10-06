"""The session engine's gaps (roadmap phase 3; spec: the vault note "Session
Engine - Phase 3 (Build Spec)"). What these prove:

- every model run leaves one row in `runs`: chat (sent or streamed), task
  and card, with its outcome, tools, tokens and the run that started it; a
  stop records whether it was confirmed; the saved reply names its run; a
  deleted chat takes its runs; the table is capped;
- an older database is copied whole before an upgrade, and the copy is the
  old version, untouched;
- chat-search hits say which chat, which date and which message;
- a local/API chat near a full window is compacted before its next message,
  only above the threshold, only for those models, and never at the cost of
  the message.
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
environment = tempfile.TemporaryDirectory(prefix="jarvis-session-engine-")
os.environ["JARVIS_DATA_DIR"] = environment.name

from core import memory_tools, runs, session_manager_store as store, settings as settings_store, task_scheduler  # noqa: E402
from core.external_brain import ExternalBrain  # noqa: E402
from core.providers import openai_compatible  # noqa: E402
from core.session_manager import session_manager  # noqa: E402
from services import chat_service  # noqa: E402

LOCAL = {"id": "local", "name": "Local", "kind": "local", "model": "local-model"}
CLAUDE = {"id": "claude", "name": "Claude", "kind": "claude_cli", "model": ""}


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def __call__(self, client, base_url, api_key, body):
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def tool_turn():
    return Scripted(
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "list_notes", "arguments": "{}"}}]}}],
         "usage": {"prompt_tokens": 400, "completion_tokens": 20, "total_tokens": 420}},
        {"choices": [{"message": {"role": "assistant", "content": "Nothing open."}}],
         "usage": {"prompt_tokens": 480, "completion_tokens": 30, "total_tokens": 510}})


class ChatCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        chat_service._brains.clear()
        chat_service._busy.clear()
        self.sid = session_manager.create_session("engine")["id"]
        patch.object(chat_service, "_resolve_endpoint", return_value=LOCAL).start()
        patch("core.model_endpoints.resolve_runtime", return_value=("http://fake", "local-model", None, None)).start()

    async def asyncTearDown(self):
        patch.stopall()
        chat_service._brains.clear()


class RunTests(ChatCase):
    async def test_a_sent_chat_turn_leaves_one_row_and_the_reply_names_it(self):
        with patch.object(openai_compatible, "_post_chat", new=tool_turn()):
            self.assertEqual(await chat_service.send_message(self.sid, "anything open?"), "Nothing open.")
        [row] = store.list_runs(self.sid)
        self.assertEqual((row["surface"], row["outcome"], row["tool_calls"], row["total_tokens"]), ("chat", "finished", 1, 930))
        self.assertEqual((row["endpoint_id"], row["model"], row["usage_complete"]), ("local", "local-model", 1))
        self.assertLessEqual(row["started_at"], row["ended_at"])
        self.assertEqual(session_manager.get_session(self.sid)["messages"][-1]["run_id"], row["id"])

    async def test_a_streamed_turn_and_a_failed_one(self):
        with patch.object(openai_compatible, "_post_chat", new=tool_turn()):
            text = "".join([c async for c in chat_service.stream_message(self.sid, "again") if isinstance(c, str)])
        self.assertEqual(text, "Nothing open.")
        self.assertEqual(store.list_runs(self.sid)[0]["outcome"], "finished")
        chat_service._brains.clear()
        with patch.object(openai_compatible, "_post_chat", new=Scripted(RuntimeError("the model fell over"))):
            with self.assertRaises(RuntimeError):
                _ = [c async for c in chat_service.stream_message(self.sid, "once more")]
        latest = store.list_runs(self.sid)[0]
        self.assertEqual(latest["outcome"], "failed")
        self.assertIn("fell over", latest["detail"])
        self.assertEqual(session_manager.get_session(self.sid)["messages"][-1]["run_id"], latest["id"])

    async def test_a_stop_records_whether_it_was_confirmed(self):
        streaming = asyncio.Event()

        class Slow:
            is_admin = False

            async def events(self, prompt, stream=True):
                yield runs.text("partial")
                streaming.set()
                await asyncio.sleep(30)

            async def cancel(self):
                return runs.StopResult(False, "a tool may still be running")

            async def disconnect(self):
                pass

        async def consume():
            async for _ in chat_service.stream_message(self.sid, "go"):
                pass
        with patch.object(chat_service, "_get_brain", return_value=(Slow(), False)):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(streaming.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        row = store.list_runs(self.sid)[0]
        self.assertEqual((row["outcome"], row["stop_confirmed"]), ("stopped", 0))

    async def test_a_cards_run_and_a_parent(self):
        class Adapter:
            async def events(self, prompt, stream=True):
                yield runs.text("done")
                yield runs.usage_event({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
                yield runs.result(True)
        await task_scheduler.complete(Adapter(), "do it", {"id": "card-1", "endpoint_id": "local"}, "card")
        row = next(r for r in store.list_runs() if r["task_id"] == "card-1")
        self.assertEqual((row["surface"], row["outcome"], row["total_tokens"], row["session_id"]), ("card", "finished", 12, None))
        child = runs.RunContext("task", parent_run_id=row["id"])
        await runs.complete(Adapter(), "x", child)
        self.assertEqual(store.get_run(child.run_id)["parent_id"], row["id"])

    async def test_a_deleted_chat_takes_its_runs_and_the_table_is_capped(self):
        with patch.object(openai_compatible, "_post_chat", new=tool_turn()):
            await chat_service.send_message(self.sid, "x")
        session_manager.delete_session(self.sid)
        self.assertEqual(store.list_runs(self.sid), [])
        with patch.object(store, "RUNS_KEPT", 3):
            now = time.time() + 10
            for i in range(5):
                runs.record(runs.RunContext("task", started_at=now + i), runs.Tally(), "finished")
            self.assertEqual(len(store.list_runs(limit=100)), 3)
            self.assertEqual([r["started_at"] for r in store.list_runs(limit=100)], [now + 4, now + 3, now + 2],
                             "the newest are the ones kept")


class UpgradeTests(unittest.TestCase):
    def test_an_older_database_is_copied_whole_before_its_upgrade(self):
        folder = tempfile.mkdtemp(dir=environment.name)
        path = os.path.join(folder, "sessions.db")
        old = sqlite3.connect(path)
        old.executescript(store._SCHEMA.split("-- One row per model run")[0])
        old.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '3')")
        old.execute("INSERT INTO sessions (id, doc, title) VALUES ('s-old', '{}', 'kept')")
        old.execute("INSERT INTO meta (key, value) VALUES ('legacy_json_import_completed_at', '1')")
        old.commit()
        old.close()
        store.close()
        try:
            with patch.object(store, "DB_FILE", path):
                # An upgrade that fails part-way, after changing the file:
                # the retry must not replace the untouched copy with it.
                with patch.object(store, "_upgrade_schema", side_effect=RuntimeError("power cut")):
                    with self.assertRaises(RuntimeError):
                        store.connection()
                store._conn.execute("INSERT INTO sessions (id, doc, title) VALUES ('s-half', '{}', 'half-upgraded')")
                store._conn.commit()
                store.close()
                store.connection()
                upgraded = store.connection().execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
                store.close()
                self.assertEqual(upgraded, str(store.SCHEMA_VERSION))
                copy = sqlite3.connect(path + ".pre-v3")
                self.assertEqual(copy.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "3")
                self.assertEqual(copy.execute("SELECT title FROM sessions").fetchall(), [("kept",)],
                                 "the copy is from before the failed attempt, not after it")
                self.assertIsNone(copy.execute("SELECT 1 FROM sqlite_master WHERE name = 'runs'").fetchone(),
                                  "the copy is the old database, from before the new table")
                copy.close()
        finally:
            store.close()

    def test_a_new_database_needs_no_copy(self):
        folder = tempfile.mkdtemp(dir=environment.name)
        path = os.path.join(folder, "sessions.db")
        store.close()
        try:
            with patch.object(store, "DB_FILE", path):
                store.connection()
                store.close()
            self.assertEqual(sorted(os.listdir(folder)), sorted(n for n in os.listdir(folder) if ".pre-v" not in n))
        finally:
            store.close()


class SearchTests(unittest.TestCase):
    def test_hits_say_which_chat_date_and_message(self):
        sid = session_manager.create_session("Planning the week")["id"]
        session_manager.append_message(sid, "user", "first message")
        session_manager.append_message(sid, "user", "the quartz deadline is Friday")
        text = memory_tools.format_session_hits(memory_tools.search_sessions("quartz deadline"))
        self.assertRegex(text, r"^\[Planning the week, \d{4}-\d{2}-\d{2}, message 2\] \(user\): ")
        hit = {"index": 0, "role": "user", "snippet": "x", "ts": None}
        self.assertEqual(memory_tools.format_archive_hits([hit]), "[earlier in this chat, message 1] (user): x",
                         "no time, no date: nothing invented")


class AutoCompactTests(ChatCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        settings_store.update_settings(auto_compact=True)
        self.addCleanup(lambda: settings_store.update_settings(auto_compact=True))
        self.compacted = []

        async def compact(session_id, keep_tail=chat_service.COMPACTION_TAIL_KEEP):
            self.compacted.append(len(session_manager.get_session(session_id)["messages"]))
            self.kept = keep_tail
            return {"archived": 4}
        patch.object(chat_service, "_compact_session", new=compact).start()

    def fill(self, percent, size=10):
        for i in range(8):
            session_manager.append_message(self.sid, "user" if i % 2 == 0 else "assistant", f"turn {i} " + "x" * size)
        session_manager.set_context_state(self.sid, {"used_tokens": 1, "percent": percent, "capacity_tokens": 8192})

    async def send(self):
        with patch.object(openai_compatible, "_post_chat", new=tool_turn()):
            return await chat_service.send_message(self.sid, "next")

    async def test_near_a_full_window_it_compacts_before_the_message_goes_in(self):
        self.fill(90)
        self.assertEqual(await self.send(), "Nothing open.")
        self.assertEqual(self.compacted, [8], "compacted before the new message was saved")
        self.assertEqual(self.kept, chat_service.COMPACTION_TAIL_KEEP, "short messages: the usual six are kept")

    async def test_long_recent_messages_are_not_all_kept(self):
        """Found live: six long kept messages left an 8,192-token chat at 96%
        after compacting. Kept messages now fit in 40% of the window."""
        self.fill(90, size=4000)
        await self.send()
        self.assertEqual(self.kept, 3, "three 4,000-character messages fit in 40% of 8,192 tokens; a fourth does not")
        self.assertEqual(chat_service._auto_tail([{"content": "y" * 100000}] * 5, 8192), 2,
                         "never fewer than the last exchange")
        rounds = {"tool_rounds": {"endpoint_id": "local", "messages": [{"role": "tool", "content": "z" * 6000}]}}
        short = [{"content": "a line"}, {"content": "a line", **rounds}] * 3
        self.assertEqual(chat_service._auto_tail(short, 8192), 4,
                         "a short reply's replayed tool results count toward what is kept")
        self.assertEqual(chat_service._auto_tail([{"content": "w" * 3000}] * 6, 8192), 4)
        self.assertEqual(chat_service._auto_tail([{"content": "w" * 3000}] * 6, 8192, overhead_chars=20000), 2,
                         "what is kept fits in the room left after the system prompt and tools")
        brain = ExternalBrain("http://fake", "m", None, session_id=self.sid, is_admin=True)
        chat_service._brains[self.sid] = brain
        measured = chat_service._fixed_prompt_chars(self.sid)
        self.assertGreater(measured, len(json.dumps(brain.tools)), "the tool list and the system message, measured")

    async def test_only_above_the_threshold_only_local_or_api_only_when_on(self):
        self.fill(80)
        await self.send()
        self.assertEqual(self.compacted, [])
        session_manager.set_context_state(self.sid, {"used_tokens": 1, "percent": 95})
        settings_store.update_settings(auto_compact=False)
        await self.send()
        self.assertEqual(self.compacted, [], "the setting turns it off")
        settings_store.update_settings(auto_compact=True)
        session_manager.set_context_state(self.sid, {"used_tokens": 1, "percent": 95})
        with patch.object(chat_service, "_resolve_endpoint", return_value=CLAUDE):
            self.assertFalse(await chat_service._compact_if_nearly_full(self.sid), "Claude Code compacts itself")
        session_manager.set_context_state(self.sid, {"used_tokens": 1, "percent": None})
        self.assertFalse(await chat_service._compact_if_nearly_full(self.sid), "an unknown window is never guessed")

    async def test_a_failed_compaction_never_costs_the_message(self):
        self.fill(95)

        async def broken(session_id):
            raise RuntimeError("the summariser fell over")
        with patch.object(chat_service, "_compact_session", new=broken):
            self.assertEqual(await self.send(), "Nothing open.")


if __name__ == "__main__":
    unittest.main()
