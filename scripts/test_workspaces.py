"""Workspaces, roadmap phase 7 (core/brain.py's fence, core/codex_brain.py's
agent sandbox, core/tool_registry.py; spec: the vault note "Workspaces -
Phase 7 (Build Spec)").

What these prove, as an attacker would try it: outside an admin chat,
Claude's file tools cannot read or write past this work's folders - not
with an absolute path, a relative one, "..", or a junction planted inside
the vault - and are refused without anyone being asked; a Codex agent run
is sandboxed with only its own folders writable; JARVIS's own source can be
rewritten only from an admin chat; and every call that changes something,
runs code or reaches another service is in the audit with who made it, what
it touched and how it ended.

Runs against a throwaway data folder; no model is called.
Run: python scripts/test_workspaces.py
"""
import asyncio
import dataclasses
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_DATA = tempfile.TemporaryDirectory(prefix="jarvis-workspaces-test-", ignore_cleanup_errors=True)
os.environ["JARVIS_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import PermissionResultDeny  # noqa: E402

from core import image_gen, permissions, session_manager_store, system_prompt, tool_registry  # noqa: E402
from core.brain import Brain  # noqa: E402
from core.codex_brain import CodexBrain  # noqa: E402
from services.agent_service import AGENTS_DIR  # noqa: E402

VAULT = os.path.join(_DATA.name, "vault")
SECRET = os.path.join(_DATA.name, "auth.json")
os.makedirs(VAULT, exist_ok=True)
Path(SECRET).write_text('{"users": {}}', encoding="utf-8")


class Context:
    suggestions = []
    title = description = None


class ClaudeFenceTests(unittest.TestCase):
    def brain(self, **kwargs):
        return Brain(vault_dir=VAULT, **kwargs)

    def refused(self, brain, tool, args):
        return brain._fence_refusal(tool, args)

    def test_outside_an_admin_chat_reads_and_writes_stay_inside(self):
        brain = self.brain(is_admin=False)
        for tool, args in (("Read", {"file_path": SECRET}),
                           ("Write", {"file_path": os.path.join(_DATA.name, "outside.txt"), "content": "x"}),
                           ("Edit", {"file_path": SECRET, "old_string": "a", "new_string": "b"}),
                           ("NotebookEdit", {"notebook_path": os.path.join(_DATA.name, "n.ipynb")}),
                           ("Grep", {"pattern": "x", "path": _DATA.name}),
                           ("Glob", {"pattern": "*", "path": os.path.dirname(_DATA.name)}),
                           ("Write", {"file_path": os.path.join(VAULT, "..", "auth.json")}),
                           ("Read", {"file_path": "..\\auth.json"})):
            self.assertIn("Not allowed", self.refused(brain, tool, args) or "", f"{tool} {args}")
        for tool, args in (("Write", {"file_path": os.path.join(VAULT, "note.md")}),
                           ("Read", {"file_path": "Projects/plan.md"}),
                           ("Glob", {"pattern": "**/*.md"}),
                           ("Write", {"file_path": os.path.join(image_gen.GENERATED_FILES_DIR, "report.docx")})):
            self.assertIsNone(self.refused(brain, tool, args), f"{tool} {args}")

    def test_a_junction_inside_the_vault_does_not_lead_out(self):
        if sys.platform != "win32":
            self.skipTest("Windows junction")
        import _winapi
        link = os.path.join(VAULT, "shortcut")
        if not os.path.exists(link):
            _winapi.CreateJunction(_DATA.name, link)
        self.addCleanup(lambda: os.path.exists(link) and os.rmdir(link))
        self.assertIn("Not allowed", self.refused(self.brain(is_admin=False), "Read", {"file_path": os.path.join(link, "auth.json")}))

    def test_an_agent_run_also_reaches_its_own_folder_but_not_another_agents(self):
        brain = self.brain(is_admin=False, agent_id="aaaaaaaaaa")
        self.assertIsNone(self.refused(brain, "Write", {"file_path": os.path.join(AGENTS_DIR, "aaaaaaaaaa", "work.md")}))
        self.assertIn("Not allowed", self.refused(brain, "Write", {"file_path": os.path.join(AGENTS_DIR, "bbbbbbbbbb", "memory.md")}))
        self.assertIn("Not allowed", self.refused(brain, "Read", {"file_path": os.path.join(AGENTS_DIR, "inbox.json")}))

    def test_an_admin_chat_is_not_fenced(self):
        self.assertIsNone(self.refused(self.brain(is_admin=True), "Write", {"file_path": SECRET}))

    def test_a_fenced_call_is_refused_without_asking_anyone(self):
        brain = self.brain(is_admin=False, agent_id="aaaaaaaaaa")
        with patch.object(permissions, "decide") as decide:
            result = asyncio.run(brain._permission("Write", {"file_path": SECRET, "content": "x"}, Context()))
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertIn("outside the folders", result.message)
        decide.assert_not_called()


class CodexAgentSandboxTests(unittest.TestCase):
    def test_an_agent_run_is_sandboxed_with_only_its_own_folders_writable(self):
        args = CodexBrain(agent_auto=True, agent_id="cccccccccc")._build_args("codex")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertEqual(args[args.index("-s") + 1], "workspace-write")
        self.assertIn('approval_policy="never"', args)
        roots = next(a for a in args if a.startswith("sandbox_workspace_write.writable_roots="))
        self.assertIn("generated_files", roots)
        self.assertIn("cccccccccc", roots)
        self.assertNotIn("tabs", roots, "no custom-tab source")
        self.assertNotIn("--add-dir", args, "no repo folders")

    def test_its_instructions_say_so(self):
        text = system_prompt.for_codex(sys.executable, "cli.py", is_admin=False, full_access=False, agent=True)
        self.assertIn("workspace sandbox", text)
        self.assertNotIn("sandbox are disabled", text)

    def test_an_admin_chat_in_auto_keeps_full_access(self):
        brain = CodexBrain(is_admin=True)
        brain.permission_mode = "auto"
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", brain._build_args("codex"))


class SourceTests(unittest.TestCase):
    def test_only_an_admin_can_rewrite_jarvis_source(self):
        self.assertTrue(tool_registry._REGISTRY["write_repo_file"].admin_only)
        for ctx in (tool_registry.ToolContext(), tool_registry.ToolContext(agent_id="aaaaaaaaaa"),
                    tool_registry.ToolContext(session_id="s")):
            self.assertEqual(asyncio.run(tool_registry.call("write_repo_file", {"path": "core/x.py", "content": ""},
                                                            ctx, tool_registry.OPENAI)), "Unknown tool: write_repo_file")


class AuditTests(unittest.TestCase):
    def test_every_change_code_run_and_outside_call_is_recorded(self):
        """Every non-read tool, whatever it is, through the dispatcher:
        principal (by), scope (content) and outcome (decision)."""
        outcomes = iter(["done", "Not run: no", "boom"])

        async def handler(args, ctx):
            outcome = next(outcomes)
            if outcome == "boom":
                raise RuntimeError("failed")
            return outcome

        writers = [s for s in tool_registry._REGISTRY.values() if s.effect != tool_registry.READ]
        self.assertGreater(len(writers), 15)
        for spec in writers:
            outcomes = iter(["done", "Not run: no", "boom"])
            ctx = tool_registry.ToolContext(session_id="chat-7", is_admin=True, agent_id="aaaaaaaaaa")
            surface = next(iter(spec.surfaces))
            with patch.dict(tool_registry._REGISTRY, {spec.name: dataclasses.replace(spec, handler=handler)}):
                before = len(permissions.audit())
                for _ in range(3):
                    asyncio.run(tool_registry.call(spec.name, {"path": "a.txt", "title": "t"}, ctx, surface))
            entries = permissions.audit()[before:]
            self.assertEqual([e["decision"] for e in entries], ["tool used", "tool refused", "tool failed"], spec.name)
            for entry in entries:
                self.assertEqual((entry["tool"], entry["effect"]), (spec.name, spec.effect))
                self.assertIn("chat:chat-7", entry["by"], "who")
                self.assertIn("content", entry, "what it touched")


def tearDownModule():
    session_manager_store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
