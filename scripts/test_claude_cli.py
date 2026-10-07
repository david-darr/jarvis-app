"""Selection of updating Claude executables and the packaged fallback."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import claude_cli


class ClaudeCLISelectionTests(unittest.TestCase):
    def setUp(self):
        claude_cli._bundled_version.cache_clear()
        self.addCleanup(claude_cli._bundled_version.cache_clear)
        root = Path(__file__).resolve().parents[1] / "test-claude-cli-fixture"
        self.home = root / "home"
        self.bundled = root / "sdk" / "_bundled" / "claude.exe"
        self.native = self.home / ".local" / "bin" / "claude.exe"
        self.files = {self.bundled, self.native}
        patches = (
            patch.object(claude_cli.claude_agent_sdk, "__file__", str(self.bundled.parent.parent / "__init__.py")),
            patch.object(claude_cli.Path, "home", return_value=self.home),
            patch.object(claude_cli.Path, "is_file", autospec=True, side_effect=lambda path: path in self.files),
            patch.object(claude_cli.platform, "system", return_value="Windows"),
            patch.object(claude_cli.shutil, "which", return_value=None),
        )
        for change in patches:
            change.start()
            self.addCleanup(change.stop)

    def test_newer_native_cli_is_used(self):
        versions = {self.bundled: (2, 1, 286), self.native: (2, 1, 287)}
        with patch.object(claude_cli, "_version", side_effect=versions.get):
            self.assertEqual(claude_cli.preferred_cli_path(), str(self.native))

    def test_older_or_broken_native_cli_keeps_packaged_fallback(self):
        for native_version in ((2, 1, 273), None):
            with self.subTest(native_version=native_version):
                versions = {self.bundled: (2, 1, 286), self.native: native_version}
                with patch.object(claude_cli, "_version", side_effect=versions.get):
                    self.assertIsNone(claude_cli.preferred_cli_path())

    def test_macos_native_launcher_is_selected(self):
        bundled = self.bundled.with_name("claude")
        native = self.native.with_name("claude")
        self.files.update((bundled, native))
        versions = {bundled: (2, 1, 281), native: (2, 1, 287)}
        with (patch.object(claude_cli.platform, "system", return_value="Darwin"),
              patch.object(claude_cli, "_version", side_effect=versions.get)):
            self.assertEqual(claude_cli.preferred_cli_path(), str(native))

    def test_npm_executable_is_used_without_launching_its_batch_shim(self):
        self.files.remove(self.native)
        shim = self.home / "npm" / "claude.cmd"
        executable = shim.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        self.files.add(executable)
        versions = {self.bundled: (2, 1, 281), executable: (2, 1, 287)}
        with (patch.object(claude_cli.shutil, "which", return_value=str(shim)),
              patch.object(claude_cli, "_version", side_effect=versions.get)):
            self.assertEqual(claude_cli.preferred_cli_path(), str(executable))
            versions[executable] = (2, 1, 280)
            self.assertIsNone(claude_cli.preferred_cli_path())

    def test_npm_batch_shim_without_executable_keeps_fallback(self):
        self.files.remove(self.native)
        shim = self.home / "npm" / "claude.cmd"
        with (patch.object(claude_cli.shutil, "which", return_value=str(shim)),
              patch.object(claude_cli, "_version", return_value=(2, 1, 281))):
            self.assertIsNone(claude_cli.preferred_cli_path())


if __name__ == "__main__":
    unittest.main()
