"""Bundled updates preserve edits, deletions and pre-existing user skills."""
import hashlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.tab_test_support import TemporaryDirectory
environment = TemporaryDirectory(prefix="skill-seed-")
os.environ["JARVIS_DATA_DIR"] = environment.name
from core import settings
from services import skills_service


class SkillSeedTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory(dir=environment.name)
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.templates, self.skills = root / "templates", root / "skills"
        self.template = self.templates / "example" / "SKILL.md"
        self.target = self.skills / "example" / "SKILL.md"
        self.template.parent.mkdir(parents=True)
        self.template.write_text("Bundled v1\n", encoding="utf-8")
        self.state = {}
        for mocked in (
            patch.object(skills_service, "SKILL_TEMPLATES_DIR", str(self.templates)),
            patch.object(skills_service, "SKILLS_DIR", str(self.skills)),
            patch.object(settings, "get_setting", side_effect=lambda key: self.state.get(key)),
            patch.object(settings, "update_settings", side_effect=lambda **fields: self.state.update(fields)),
            patch("services.skill_curator.record"),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_unchanged_seed_refreshes_across_repeated_updates(self):
        self.assertEqual(skills_service.seed_default_skills(), ["example"])
        for version in (2, 3):
            content = f"Bundled v{version}\n"
            self.template.write_text(content, encoding="utf-8")
            self.assertEqual(skills_service.seed_default_skills(), [])
            self.assertEqual(self.target.read_text(encoding="utf-8"), content)
        self.assertEqual(self.state["seeded_skills"], ["example"])

    def test_user_edit_and_deletion_survive_updates(self):
        skills_service.seed_default_skills()
        self.target.write_text("My edits\n", encoding="utf-8")
        self.template.write_text("Bundled v2\n", encoding="utf-8")
        skills_service.seed_default_skills()
        self.assertEqual(self.target.read_text(encoding="utf-8"), "My edits\n")
        self.target.unlink()
        skills_service.seed_default_skills()
        self.assertFalse(self.target.exists())

    def test_known_legacy_seed_refreshes_but_edited_legacy_does_not(self):
        self.target.parent.mkdir(parents=True)
        self.state["seeded_skills"] = ["example"]
        # A Windows checkout and an LF template describe the same seed.
        self.target.write_bytes(b"Bundled v1\r\n")
        old_hash = hashlib.sha256(b"Bundled v1\n").hexdigest()
        self.template.write_text("Bundled v2\n", encoding="utf-8")
        with patch.object(skills_service, "_LEGACY_SEED_HASHES", {"example": {old_hash}}):
            skills_service.seed_default_skills()
            self.assertEqual(self.target.read_text(encoding="utf-8"), "Bundled v2\n")
            self.state.pop("seeded_skill_hashes")
            self.target.write_text("Bundled v1 with my changes\n", encoding="utf-8")
            skills_service.seed_default_skills()
            self.assertEqual(self.target.read_text(encoding="utf-8"), "Bundled v1 with my changes\n")

    def test_preexisting_user_copy_never_becomes_an_updatable_seed(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("My skill\n", encoding="utf-8")
        skills_service.seed_default_skills()
        self.assertNotIn("example", self.state["seeded_skill_hashes"])
        self.template.write_text("Bundled v2\n", encoding="utf-8")
        skills_service.seed_default_skills()
        self.assertEqual(self.target.read_text(encoding="utf-8"), "My skill\n")

    def test_unreadable_copy_does_not_break_seeding_other_skills(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"\xff")
        self.state["seeded_skills"] = ["example"]
        other = self.templates / "other" / "SKILL.md"
        other.parent.mkdir()
        other.write_text("Another skill\n", encoding="utf-8")
        with self.assertLogs(skills_service.logger, level="ERROR"):
            self.assertEqual(skills_service.seed_default_skills(), ["other"])
        self.assertEqual(self.target.read_bytes(), b"\xff")


if __name__ == "__main__":
    unittest.main()
