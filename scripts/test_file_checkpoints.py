"""Focused tests for file checkpoints and rollback."""
import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import file_checkpoints as cp


class FileCheckpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture_dir = Path(__file__).resolve().parents[1] / "data"
        fixture_dir.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixture_dir)
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.vault = base / "vault"
        self.repo = base / "source"
        self.work = base / "work"
        for folder in (self.vault, self.repo, self.work):
            folder.mkdir()
        (self.repo / ".git").write_text("test worktree marker")
        self.patches = [patch.object(cp, "STORE", base / "archive"),
                        patch.object(cp, "BASE_DIR", str(self.repo)),
                        patch.object(cp, "resolve_vault_dir", return_value=str(self.vault))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_git_objects_and_restore_added_modified_deleted_files(self):
        (self.vault / "changed.md").write_text("original\n")
        (self.work / "deleted.txt").write_text("remove me\n")
        async with cp.around_turn("chat:abc", str(self.work)):
            (self.vault / "changed.md").write_text("edited\n")
            (self.work / "deleted.txt").unlink()
            (self.repo / "added.py").write_text("print('new')\n")
        events = cp.list_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["status"], "changed")
        self.assertEqual(len(event["roots"]), 3)
        full = cp.get_event(event["id"])
        self.assertIn("-original", full["roots"][0]["changes"][0]["diff"])
        vault_root = next(r for r in full["roots"] if r["path"] == str(self.vault))
        result = subprocess.run(["git", f"--git-dir={cp._repo(self.vault)}", "cat-file", "-t", vault_root["before"]],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "commit")
        tree = subprocess.run(["git", f"--git-dir={cp._repo(self.vault)}", "ls-tree", "-r", vault_root["after"]],
                              capture_output=True, text=True, check=True)
        self.assertIn("changed.md", tree.stdout)
        choices = [{"root": root["path"], "path": change["path"]}
                   for root in full["roots"] for change in root["changes"]]
        with self.assertRaises(cp.Conflict):
            cp.restore(event["id"], choices + choices[:1])
        self.assertTrue((self.repo / "added.py").exists())
        self.assertEqual(len(cp.restore(event["id"], choices)), 3)
        self.assertEqual((self.vault / "changed.md").read_text(), "original\n")
        self.assertEqual((self.work / "deleted.txt").read_text(), "remove me\n")
        self.assertFalse((self.repo / "added.py").exists())
        reviewed = cp.get_event(event["id"])
        self.assertTrue(all(change["restored_at"] for root in reviewed["roots"] for change in root["changes"]))
        self.assertTrue(all(change["restore_state"] == "restored" for root in reviewed["roots"] for change in root["changes"]))
        with self.assertRaisesRegex(cp.Conflict, "Already restored"):
            cp.restore(event["id"], choices[:1])

    async def test_existing_restore_is_recognized_without_saved_status(self):
        async with cp.around_turn("chat:abc"):
            (self.vault / "note.md").write_text("test")
        event = cp.list_events()[0]
        (self.vault / "note.md").unlink()  # simulates a restore done by the earlier version
        reviewed = cp.get_event(event["id"])
        self.assertEqual(reviewed["roots"][0]["changes"][0]["restore_state"], "at_before")
        with self.assertRaisesRegex(cp.Conflict, "Already at previous state"):
            cp.restore(event["id"], [{"root": str(self.vault), "path": "note.md"}])

    async def test_partial_restore_leaves_other_files_available(self):
        async with cp.around_turn("chat:abc"):
            (self.vault / "first.md").write_text("one")
            (self.vault / "second.md").write_text("two")
        event = cp.list_events()[0]
        cp.restore(event["id"], [{"root": str(self.vault), "path": "first.md"}])
        changes = {c["path"]: c for c in cp.get_event(event["id"])["roots"][0]["changes"]}
        self.assertEqual(changes["first.md"]["restore_state"], "restored")
        self.assertEqual(changes["second.md"]["restore_state"], "available")

    async def test_later_edit_blocks_entire_selection(self):
        async with cp.around_turn("chat:abc"):
            (self.vault / "note.md").write_text("model wrote this")
            (self.repo / "file.py").write_text("model wrote this")
        event = cp.list_events()[0]
        (self.vault / "note.md").write_text("later edit")
        choices = [{"root": root["path"], "path": change["path"]}
                   for root in event["roots"] for change in root["changes"]]
        with self.assertRaises(cp.Conflict):
            cp.restore(event["id"], choices)
        self.assertTrue((self.repo / "file.py").exists())
        self.assertEqual((self.vault / "note.md").read_text(), "later edit")

    async def test_cancelled_turn_saves_after_image(self):
        with self.assertRaises(asyncio.CancelledError):
            async with cp.around_turn("chat:stopped"):
                (self.vault / "partial.md").write_text("partial")
                raise asyncio.CancelledError()
        event = cp.list_events()[0]
        self.assertEqual(event["status"], "changed")
        self.assertEqual(event["roots"][0]["changes"][0]["path"], "partial.md")

    async def test_oversize_is_reported_without_false_deletion(self):
        (self.vault / "large.bin").write_bytes(b"old")
        with patch.object(cp, "MAX_FILE", 4):
            async with cp.around_turn("chat:abc"):
                (self.vault / "large.bin").write_bytes(b"new content")
        event = cp.list_events()[0]
        vault = next(r for r in event["roots"] if r["path"] == str(self.vault))
        self.assertEqual(vault["changes"], [])
        self.assertEqual(vault["skipped"], ["large.bin"])


if __name__ == "__main__":
    unittest.main()
