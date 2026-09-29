"""Composer reference lookup, validation and model-context boundaries."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import chat_references
from services import chat_service


class FakeSessions:
    def __init__(self):
        self.sessions = {
            "source": {"id": "source", "title": "Earlier design chat", "messages": [
                {"role": "user", "content": "Use a compact navigation rail."},
                {"role": "assistant", "content": "Agreed; keep it collapsible."},
            ]},
            "current": {"id": "current", "title": "Current chat", "messages": []},
        }
        self.sent = None

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def list_sessions(self):
        return [{"id": item["id"], "title": item["title"]} for item in self.sessions.values()]

    def record_sent_text(self, session_id, index, typed, sent):
        self.sent = (session_id, index, typed, sent)

    def record_image_attachments(self, session_id, index, attachment_ids):
        pass


class ChatReferenceTests(unittest.TestCase):
    def setUp(self):
        fixture_dir = Path(__file__).resolve().parents[1] / "data"
        fixture_dir.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixture_dir)
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "brief.md"
        self.file.write_text("A real file excerpt for the model.", encoding="utf-8")
        self.sessions = FakeSessions()
        patches = [
            patch.object(chat_references, "session_manager", self.sessions),
            patch.object(chat_service, "session_manager", self.sessions),
            patch.object(chat_references.chat_files, "list_for_session", return_value=[
                {"id": "f" * 24, "name": "brief.md", "url": "/chat-files/" + "f" * 24,
                 "exists": True, "origin": "created"}]),
            patch.object(chat_references.chat_files, "resolve_local", return_value=self.file),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_search_combines_files_vault_notes_and_chats(self):
        with patch.object(chat_references.chat_files, "list_library", return_value=[
            {"session_id": "source", "title": "Earlier design chat", "files": [
                {"id": "f" * 24, "name": "brief.md", "exists": True}]}]), \
             patch.object(chat_references.memory_tools, "search_vault", return_value=[
                 {"path": "Design/Brief.md", "snippet": "design"}]), \
             patch.object(chat_references.memory_tools, "search_sessions", return_value=[]):
            results = chat_references.search("brief", "current")
        self.assertEqual({item["kind"] for item in results}, {"file", "note"})
        self.assertEqual(results[0]["session_id"], "source")
        self.assertEqual(results[1]["id"], "Design/Brief.md")
        with patch.object(chat_references.chat_files, "list_library", return_value=[]), \
             patch.object(chat_references.memory_tools, "search_vault", return_value=[]), \
             patch.object(chat_references.memory_tools, "search_sessions", return_value=[]):
            chats = chat_references.search("design", "current")
        self.assertEqual([(item["kind"], item["id"]) for item in chats], [("chat", "source")])

    def test_resolve_uses_current_file_bytes_and_fences_each_source(self):
        refs = [{"kind": "file", "id": "f" * 24, "session_id": "source", "label": "brief.md"},
                {"kind": "note", "id": "Design/Brief.md", "label": "Brief"},
                {"kind": "chat", "id": "source", "label": "Earlier design chat"}]
        with patch.object(chat_references.memory_tools, "read_vault_file", return_value="Vault note contents"):
            context = chat_references.resolve("current", refs)
        self.assertIn("A real file excerpt for the model.", context)
        self.assertIn("Vault note contents", context)
        self.assertIn("Use a compact navigation rail.", context)
        self.assertEqual(context.count("<<<UNTRUSTED-"), 3)
        self.assertEqual(len(chat_references.metadata(refs)), 3)
        sent = chat_service._prepare_sent_text("current", 0, "Summarize these", [], context)
        self.assertIn(context, sent)
        self.assertEqual(self.sessions.sent[:3], ("current", 0, "Summarize these"))

    def test_invalid_and_stale_references_are_rejected(self):
        bad = [
            [{"kind": "note", "id": "../outside.md"}],
            [{"kind": "chat", "id": "current"}],
            [{"kind": "file", "id": "missing", "session_id": "source"}],
            [{"kind": [], "id": "source"}],
            [{"kind": "chat", "id": []}],
            [{"kind": "chat", "id": "source"}] * 6,
        ]
        for refs in bad:
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                chat_references.resolve("current", refs)

    def test_large_chat_is_bounded_and_duplicate_reference_is_deduplicated(self):
        self.sessions.sessions["source"]["messages"] = [
            {"role": "user", "content": "older " * 4000},
            {"role": "assistant", "content": "recent detail"},
        ]
        ref = {"kind": "chat", "id": "source"}
        context = chat_references.resolve("current", [ref, ref])
        self.assertIn("recent detail", context)
        self.assertIn("Earlier messages omitted", context)
        self.assertEqual(context.count("<<<UNTRUSTED-"), 1)
        self.assertLess(len(context), chat_references.MAX_TOTAL_CHARS)


if __name__ == "__main__":
    unittest.main()
