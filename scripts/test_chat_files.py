"""Per-chat file ownership, attachment and native-write indexing tests."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import attachments, chat_artifacts, chat_files, file_checkpoints, image_gen


class FakeSessions:
    def __init__(self, vault):
        self.sessions = {
            "chat-one": {"id": "chat-one", "title": "First chat", "workspace_dir": str(vault),
                         "messages": [], "chat_files": [], "artifact_urls": [], "updated_at": 1},
            "chat-two": {"id": "chat-two", "title": "Second chat", "workspace_dir": str(vault),
                         "messages": [], "chat_files": [], "artifact_urls": [], "updated_at": 2},
        }

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def register_chat_file(self, session_id, entry):
        files = self.sessions[session_id]["chat_files"]
        if not any(item["id"] == entry["id"] for item in files):
            files.append(entry)

    def list_sessions(self):
        return [{"id": item["id"], "title": item["title"]} for item in self.sessions.values()]


class ChatFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture_dir = Path(__file__).resolve().parents[1] / "data"
        fixture_dir.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixture_dir)
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.vault = base / "vault"
        self.source = base / "source"
        self.generated = base / "generated"
        for folder in (self.vault, self.source, self.generated):
            folder.mkdir()
        (self.source / ".git").write_text("worktree marker")
        self.manager = FakeSessions(self.vault)
        patches = [patch.object(chat_files, "session_manager", self.manager),
                   patch.object(chat_artifacts, "session_manager", self.manager),
                   patch.object(file_checkpoints, "STORE", base / "archive"),
                   patch.object(file_checkpoints, "BASE_DIR", str(self.source)),
                   patch.object(file_checkpoints, "resolve_vault_dir", return_value=str(self.vault)),
                   patch.object(image_gen, "GENERATED_FILES_DIR", str(self.generated))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_attachment_preview_is_limited_to_own_chat(self):
        folder = self.vault / ".attachments" / "chat-one"
        folder.mkdir(parents=True)
        (folder / "upload.txt").write_text("sent")
        chat_files.record_local("chat-one", str(self.vault), ".attachments/chat-one/upload.txt", "attachment")
        files = chat_files.list_for_session("chat-one")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["origin"], "attachment")
        path, metadata = chat_artifacts.resolve("chat-one", files[0]["url"])
        self.assertEqual(path.read_text(), "sent")
        self.assertEqual(metadata["filename"], "upload.txt")
        with self.assertRaises(HTTPException):
            chat_artifacts.resolve("chat-two", files[0]["url"])
        self.assertEqual(chat_files.list_library()[0]["title"], "First chat")

    async def test_checkpoint_indexes_native_created_file(self):
        async with file_checkpoints.around_turn("chat:chat-one"):
            (self.vault / "created.md").write_text("written by a CLI")
        files = chat_files.list_for_session("chat-one")
        created = next(file for file in files if file["name"] == "created.md")
        self.assertEqual(created["origin"], "created")
        self.assertTrue(created["exists"])
        self.assertEqual(chat_artifacts.resolve("chat-one", created["url"])[0].read_text(), "written by a CLI")
        (self.vault / "created.md").unlink()
        self.assertFalse(chat_files.list_for_session("chat-one")[0]["exists"])

    async def test_large_created_file_is_listed_without_checkpoint_blob(self):
        with patch.object(file_checkpoints, "MAX_FILE", 1):
            async with file_checkpoints.around_turn("chat:chat-one"):
                (self.vault / "large.bin").write_bytes(b"large")
        self.assertIn("large.bin", [file["name"] for file in chat_files.list_for_session("chat-one")])

    async def test_generated_and_legacy_attachment_files(self):
        self.manager.sessions["chat-one"]["artifact_urls"].append("/generated-files/report.txt")
        (self.generated / "report.txt").write_text("report")
        folder = self.vault / ".attachments" / "chat-one"
        folder.mkdir(parents=True)
        (folder / "old.txt").write_text("old upload")
        files = chat_files.list_for_session("chat-one")
        self.assertEqual({file["origin"] for file in files}, {"attachment", "generated"})
        self.assertTrue(all(file["exists"] for file in files))

    async def test_upload_shows_original_once(self):
        from services import chat_service
        staging = Path(self.temp.name) / "staging"
        with patch.object(attachments, "STAGING_DIR", str(staging)), \
             patch.object(chat_service, "session_manager", self.manager):
            staged = attachments.stage_file("upload.txt", b"original")
            chat_service._apply_attachments("chat-one", "read this", [staged["id"]])
            files = chat_files.list_for_session("chat-one")
        self.assertEqual([(file["name"], file["origin"]) for file in files], [("upload.txt", "attachment")])
        self.assertEqual(chat_artifacts.resolve("chat-one", files[0]["url"])[0].read_bytes(), b"original")

    async def test_tampered_path_cannot_escape_root(self):
        self.manager.sessions["chat-one"]["chat_files"].append({
            "id": "a" * 24, "root": str(self.vault), "path": "../outside.txt",
            "name": "outside.txt", "origin": "created", "created_at": 1})
        with self.assertRaises(HTTPException):
            chat_files.resolve_local("chat-one", "a" * 24)


if __name__ == "__main__":
    unittest.main()
