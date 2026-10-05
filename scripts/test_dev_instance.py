"""scripts/dev_instance.py: a development copy of JARVIS made from a real
data folder must leave that folder untouched and must not be able to act on
the outside world, or reach back into the real app, by itself.

Every test builds a synthetic "real" data folder in a temp directory; nothing
here reads the actual %APPDATA%\\JARVIS. Run: python scripts/test_dev_instance.py
"""
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import dev_instance  # noqa: E402

# Builds the sessions the way the app does, in the source folder's own process.
MAKE_SESSIONS = r"""
import sys
sys.path.insert(0, sys.argv[1])
from core.session_manager import session_manager
cli = session_manager.create_session("Claude chat")["id"]
session_manager.append_message(cli, "user", "remember amber-falcon")
session_manager.set_claude_session(cli, "real-claude-thread", 1)
codex = session_manager.create_session("Codex chat")["id"]
doc = session_manager.get_session(codex)
from core import session_manager_store as store
doc["codex_thread_id"] = "real-codex-thread"
store.save_session(doc, rebuild_messages_from=store.MESSAGES_UNCHANGED)
print(cli, codex)
"""


def tree_digest(root: Path) -> dict:
    """Every file's bytes. SQLite's sidecars are the exception: a read-only
    reader of a WAL database may create its shared-memory index and an empty
    write-ahead log, which hold no data. A WAL with anything in it counts."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.name.endswith("-shm")
            and not (p.name.endswith("-wal") and p.stat().st_size == 0)}


class DevInstanceCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-devinstance-test-"))
        self.appdata = self.tmp / "appdata"
        self.source = self.appdata / "JARVIS" / "data"
        self.vault = self.tmp / "Obsidian Vault"
        self.root = self.appdata / "JARVIS-dev"
        self.target = self.root / "data"
        self.port = self._free_port()
        self._make_source()

    def tearDown(self):
        if getattr(self, "held", None):
            self.held.close()
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(self.tmp)] if os.name == "nt" else ["rm", "-rf", str(self.tmp)],
                       capture_output=True)

    @staticmethod
    def _free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _write(self, name, value):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def _make_source(self):
        self.source.mkdir(parents=True)
        (self.vault / "Projects").mkdir(parents=True)
        (self.vault / "Projects" / "Plan.md").write_text("# Plan\nreal vault note", encoding="utf-8")
        self._write("settings.json", {"vault_dir": str(self.vault), "remote_access_enabled": True,
                                      "remote_access_port": 6767, "auto_enabled_builtins": [],
                                      "discord_bot_token_encrypted": "legacy-token"})
        self._write("tasks.json", {
            "daily": {"id": "daily", "name": "Daily brief", "schedule_kind": "daily", "enabled": True},
            "off": {"id": "off", "name": "Old job", "schedule_kind": "interval", "enabled": False},
            "ready": {"id": "ready", "name": "Ready card", "schedule_kind": "card", "status": "ready", "enabled": True},
            "running": {"id": "running", "name": "Running card", "schedule_kind": "card", "status": "running",
                        "enabled": True, "claimed_until": 9e9, "run_started_at": 1.0},
            "review": {"id": "review", "name": "Review card", "schedule_kind": "card", "status": "review", "enabled": True},
        })
        self._write("discord_bots.json", {"b1": {"id": "b1", "name": "JARVIS bot", "token_encrypted": "x"}})
        self._write("logs/backend.log", "noise")
        env = {**os.environ, "JARVIS_DATA_DIR": str(self.source)}
        out = subprocess.run([sys.executable, "-c", MAKE_SESSIONS, str(REPO)], env=env, cwd=REPO,
                             capture_output=True, text=True, check=True)
        self.claude_chat, self.codex_chat = out.stdout.split()[-2:]
        self._make_swarm()
        # A database the real app is writing to right now: a row only in the WAL.
        live = self.source / "live.db"
        self.held = sqlite3.connect(live)
        self.held.execute("PRAGMA journal_mode=WAL")
        self.held.execute("PRAGMA wal_autocheckpoint=0")
        self.held.execute("CREATE TABLE t (v TEXT)")
        self.held.execute("INSERT INTO t VALUES ('only-in-wal')")
        self.held.commit()

    def _make_swarm(self):
        sys.path.insert(0, str(REPO))
        from core.swarm.budget import BudgetLimit
        from core.swarm.store import SwarmStore
        path = self.source / "swarm" / "swarm.sqlite3"
        store = SwarmStore(path)
        limit = BudgetLimit(10000)
        pool = store.create_pool("Account", limit)
        self.swarm_system, _ = store.create_system("local", "Night shift", "Keep going", pool_id=pool,
                                                   system_limit=limit, lead_limit=limit)
        store.create_run(self.swarm_system, "Tonight's work", limit)
        store.close()
        db = sqlite3.connect(path)
        with db:
            db.execute("UPDATE systems SET state='running', configuration=? WHERE id=?",
                       (json.dumps({"mode": "scheduled", "schedule": {"enabled": True, "auto_spend_confirmed": True}}),
                        self.swarm_system))
            db.execute("INSERT INTO runtime_lock (singleton, runtime_id, generation, lease_until) VALUES (1, 'real', 1, 9e9)")
        db.close()

    def copy(self, *extra):
        dev_instance.main(["copy", "--appdata", str(self.appdata), "--port", str(self.port), *extra])

    def read(self, name):
        return json.loads((self.target / name).read_text(encoding="utf-8"))

    def test_the_real_data_and_vault_are_never_changed(self):
        before = tree_digest(self.source), tree_digest(self.vault)
        self.copy()
        self.assertEqual((tree_digest(self.source), tree_digest(self.vault)), before)

    def test_nothing_in_the_copy_runs_by_itself(self):
        self.copy()
        tasks = self.read("tasks.json")
        self.assertFalse(tasks["daily"]["enabled"], "scheduled tasks are off")
        self.assertEqual([tasks[c]["status"] for c in ("ready", "running", "review")], ["backlog", "backlog", "review"],
                         "the dispatcher has nothing Ready; a card waiting on a person stays put")
        self.assertNotIn("run_started_at", tasks["running"])
        settings = self.read("settings.json")
        self.assertFalse(settings["remote_access_enabled"])
        self.assertIsNone(settings["discord_bot_token_encrypted"])
        sys.path.insert(0, str(REPO))
        from core.builtin_tasks import AUTO_ENABLE
        self.assertTrue(set(AUTO_ENABLE) <= set(settings["auto_enabled_builtins"]),
                        "starting the copy cannot switch on a built-in automation")
        self.assertFalse((self.target / "discord_bots.json").exists())
        self.assertTrue((self.target / "discord_bots.dev-disabled.json").exists())

    def test_swarm_schedules_are_off_and_active_companies_pause(self):
        self.copy()
        db = sqlite3.connect(self.target / "swarm" / "swarm.sqlite3")
        state, configuration = db.execute("SELECT state, configuration FROM systems WHERE id=?",
                                          (self.swarm_system,)).fetchone()
        self.assertEqual(state, "pausing")
        self.assertEqual(db.execute("SELECT state FROM runs WHERE system_id=?", (self.swarm_system,)).fetchone()[0], "paused")
        self.assertFalse(json.loads(configuration)["schedule"]["enabled"])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM runtime_lock").fetchone()[0], 0,
                         "the real app's runtime lease does not travel with the copy")
        db.close()

    def test_chats_keep_their_history_but_not_the_real_cli_threads(self):
        self.copy()
        env = {**os.environ, "JARVIS_DATA_DIR": str(self.target)}
        probe = ("import sys, json; sys.path.insert(0, sys.argv[1]); from core import session_manager_store as s; "
                 "print(json.dumps({i: s.get_session(i) for i in sys.argv[2:]}))")
        out = subprocess.run([sys.executable, "-c", probe, str(REPO), self.claude_chat, self.codex_chat],
                             env=env, cwd=REPO, capture_output=True, text=True, check=True)
        docs = json.loads(out.stdout.strip().splitlines()[-1])
        claude, codex = docs[self.claude_chat], docs[self.codex_chat]
        self.assertEqual((claude["claude_session_id"], codex["codex_thread_id"]), (None, None))
        self.assertEqual(claude["messages"][0]["content"], "remember amber-falcon")

    def test_the_copy_has_its_own_vault(self):
        self.copy()
        vault = Path(self.read("settings.json")["vault_dir"])
        self.assertEqual(vault, self.root / "vault")
        self.assertEqual((vault / "Projects" / "Plan.md").read_text(encoding="utf-8"), "# Plan\nreal vault note")

    def test_a_database_being_written_is_copied_whole(self):
        self.copy()
        db = sqlite3.connect(self.target / "live.db")
        self.assertEqual(db.execute("SELECT v FROM t").fetchall(), [("only-in-wal",)])
        db.close()
        self.assertFalse((self.target / "logs").exists(), "logs are left behind")
        self.assertEqual(self.read("dev_instance.json")["mode"], "copy")

    def test_an_existing_copy_is_moved_aside_never_deleted(self):
        self.copy()
        (self.target / "my-dev-work.txt").write_text("keep me", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.copy()
        self.copy("--replace")
        aside = [p for p in self.root.iterdir() if p.name.startswith("data.replaced-")]
        self.assertEqual(len(aside), 1)
        self.assertEqual((aside[0] / "my-dev-work.txt").read_text(encoding="utf-8"), "keep me")
        self.assertFalse((self.target / "my-dev-work.txt").exists())

    def test_a_failed_copy_is_moved_aside_so_it_never_starts(self):
        self._write("tasks.json", ["not", "a", "task", "map"])
        with self.assertRaises(SystemExit):
            self.copy()
        self.assertFalse(self.target.exists(), "start:dev would open an empty copy, not a half-made one")
        self.assertTrue(any(p.name.startswith("data.failed-") for p in self.root.iterdir()))

    def test_it_refuses_while_the_dev_copy_is_running_or_when_pointed_at_itself(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.port))
            listener.listen()
            with self.assertRaises(SystemExit):
                self.copy()
        self.assertFalse(self.target.exists())
        self.copy()
        with self.assertRaises(SystemExit):
            dev_instance.main(["copy", "--appdata", str(self.appdata), "--port", str(self.port),
                               "--source", str(self.target), "--replace"])

    def test_fresh_starts_empty(self):
        dev_instance.main(["fresh", "--appdata", str(self.appdata), "--port", str(self.port)])
        self.assertEqual([p.name for p in self.target.iterdir()], ["dev_instance.json"])


if __name__ == "__main__":
    unittest.main()
