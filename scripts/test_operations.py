"""Operations, roadmap phase 8 (core/runs.py timelines, routes/run_routes.py,
core/backup.py, scripts/legacy_profile.py; spec: the vault note "Operations -
Phase 8 (Build Spec)").

What these prove: a run keeps its steps - each tool started and finished in
pairs with its duration, quota readings, how it ended - capped, and gone with
its run; the runs routes filter and open a run with what hangs off it; a full
backup holds every store consistently and leaves keys out unless asked; a
damaged or hostile backup is refused; restoring swaps the data at start,
keeps a safety copy and the vault, and puts everything back if it fails; and
an old-format profile upgrades on a real backend start with every chat kept
and nothing rerun.

Runs against throwaway data folders; no model is called.
Run: python scripts/test_operations.py
"""
import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

_DATA = tempfile.TemporaryDirectory(prefix="jarvis-operations-test-", ignore_cleanup_errors=True)
os.environ["JARVIS_DATA_DIR"] = _DATA.name
os.environ["JARVIS_VAULT_DIR"] = os.path.join(_DATA.name, "vault")
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import backup, runs, session_manager_store as store, task_scheduler  # noqa: E402
from core.middleware import require_admin  # noqa: E402
from routes import run_routes  # noqa: E402
from services.task_service import task_service  # noqa: E402

app = FastAPI()
app.include_router(run_routes.router)
app.dependency_overrides[require_admin] = lambda: "david"


def tool_run(tally, n=1, fail_last=False):
    for i in range(n):
        tally.add(runs.tool_started(f"c{i}", "list_notes", {"q": i}))
        tally.add(runs.tool_finished(f"c{i}", not (fail_last and i == n - 1), f"result {i}"))


class TimelineTests(unittest.TestCase):
    def setUp(self):
        with store.transaction() as conn:
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM run_events")

    def test_a_run_keeps_its_tools_in_pairs_with_durations_and_how_it_ended(self):
        tally = runs.Tally()
        tool_run(tally, 2, fail_last=True)
        tally.add(runs.event(runs.EventKind.QUOTA, bucket="five_hour", used_percent=40.0))
        context = runs.RunContext("chat", session_id="chat-1", model="m")
        runs.record(context, tally, "stopped", stop=runs.StopResult(False, "the tool was running"))
        steps = store.run_events(context.run_id)
        self.assertEqual([s["kind"] for s in steps],
                         ["tool_started", "tool_finished", "tool_started", "tool_finished", "quota", "stopped"])
        self.assertEqual(steps[1]["name"], "list_notes", "a finish carries its tool's name")
        self.assertEqual((steps[1]["ok"], steps[3]["ok"]), (True, False))
        self.assertIsNotNone(steps[1]["seconds"])
        self.assertIn("the tool was running", steps[-1]["detail"])
        self.assertEqual(store.get_run(context.run_id)["tool_calls"], 2)

    def test_a_long_run_is_capped_and_says_so(self):
        tally = runs.Tally()
        tool_run(tally, runs.TIMELINE_LIMIT)
        context = runs.RunContext("task")
        runs.record(context, tally, "finished")
        steps = store.run_events(context.run_id)
        self.assertEqual(len(steps), runs.TIMELINE_LIMIT + 1)
        self.assertEqual(steps[-1]["kind"], "truncated")
        self.assertIn(str(runs.TIMELINE_LIMIT), steps[-1]["detail"])

    def test_details_are_clipped_keeping_both_ends(self):
        tally = runs.Tally()
        tally.add(runs.tool_started("x", "shell", {"command": "C:/a/very/long/path/" * 40 + "hive_mind_cli.py list_notes"}))
        detail = tally.timeline[0]["detail"]
        self.assertLessEqual(len(detail), runs.TIMELINE_CLIP + 3)
        self.assertIn("list_notes", detail, "a command's tail survives")

    def test_a_timeline_goes_with_its_run(self):
        with patch.object(store, "RUNS_KEPT", 2):
            ids = []
            for n in range(3):
                tally = runs.Tally()
                tool_run(tally)
                context = runs.RunContext("chat", session_id=f"chat-{n}", started_at=time.time() + n)
                runs.record(context, tally, "finished")
                ids.append(context.run_id)
        self.assertIsNone(store.get_run(ids[0]))
        self.assertEqual(store.run_events(ids[0]), [], "the pruned run's steps went too")
        store.delete_session("chat-2")
        self.assertEqual(store.run_events(ids[2]), [])
        self.assertTrue(store.run_events(ids[1]))


class RouteTests(unittest.TestCase):
    def setUp(self):
        with store.transaction() as conn:
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM run_events")
        self.web = TestClient(app)

    def test_listing_filters_and_one_run_opens_with_what_hangs_off_it(self):
        parent = runs.RunContext("chat", session_id="chat-9")
        child = runs.RunContext("helper", parent_run_id=parent.run_id)
        failed = runs.RunContext("task", task_id="t-1")
        for context, outcome in ((parent, "finished"), (child, "finished"), (failed, "failed")):
            tally = runs.Tally()
            tool_run(tally)
            runs.record(context, tally, outcome, detail="model down" if outcome == "failed" else "")
        self.assertEqual([r["id"] for r in self.web.get("/api/runs?outcome=failed").json()], [failed.run_id])
        self.assertEqual([r["id"] for r in self.web.get("/api/runs?session_id=chat-9").json()], [parent.run_id])
        self.assertEqual(self.web.get("/api/runs?outcome=bogus").status_code, 400)
        detail = self.web.get(f"/api/runs/{parent.run_id}").json()
        self.assertEqual([c["id"] for c in detail["children"]], [child.run_id])
        self.assertEqual([s["kind"] for s in detail["steps"]], ["tool_started", "tool_finished"])
        self.assertEqual(self.web.get(f"/api/runs/{child.run_id}").json()["parent"]["id"], parent.run_id)
        self.assertEqual(self.web.get(f"/api/runs/{failed.run_id}").json()["steps"][-1]["kind"], "failed")
        self.assertEqual(self.web.get("/api/runs/nope").status_code, 404)

    def test_a_task_run_record_names_its_run(self):
        class Brain:
            model = "m"
            async def events(self, prompt, stream=True):
                yield runs.tool_started("a", "list_notes", {})
                yield runs.tool_finished("a", True, "none")
                yield runs.text("ok")
                yield runs.result(True)

        task = task_service.create_task("timeline task", "p", "interval", interval_seconds=3600)
        self.addCleanup(task_service.delete_task, task["id"])
        task_service.mark_started(task["id"])
        with patch.object(task_scheduler, "task_endpoint_id", return_value=None):
            asyncio.run(task_scheduler.complete(Brain(), "p", task, "task"))
        task_service.record_run(task["id"], output="ok")
        record = task_service.list_runs(task["id"])[0]
        self.assertTrue(record["run_id"])
        detail = self.web.get(f"/api/runs/{record['run_id']}").json()
        self.assertEqual(detail["task_run"]["outcome"], "succeeded")
        self.assertEqual(detail["label"], "timeline task")


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="jarvis-backup-src-", dir=_DATA.name))
        (self.data / ".secret_key").write_bytes(b"k" * 44)
        (self.data / "model_endpoints.json").write_text(json.dumps(
            {"e1": {"id": "e1", "name": "API", "api_key_encrypted": "gAAAAA" + "x" * 100, "num_ctx": None}}))
        (self.data / "integrations.json").write_text(json.dumps(
            {"i1": {"oauth": {"tokens": "gAAAAA" + "y" * 100}, "url": "https://example.com"}}))
        for folder in ("logs", "file_checkpoints", "vault", "agents/a1"):
            (self.data / folder).mkdir(parents=True)
        (self.data / "logs" / "backend.log").write_text("log")
        (self.data / "vault" / "note.md").write_text("my note")
        (self.data / "agents" / "a1" / "memory.md").write_text("memory")
        db = sqlite3.connect(self.data / "sessions.db")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("CREATE TABLE sessions (id TEXT)")
        db.execute("INSERT INTO sessions VALUES ('s1')")
        db.commit()  # still open, its WAL not checkpointed: the copy must still have the row
        self.addCleanup(db.close)
        self.zip = str(self.data.parent / f"{self.data.name}.zip")

    def backup(self, include_keys=False):
        with patch("core.backup._data_dir", return_value=str(self.data)), \
                patch("core.vault.resolve_vault_dir", return_value=str(self.data / "vault")):
            return backup.make_backup(self.zip, include_keys)

    def test_everything_but_keys_logs_checkpoints_and_the_vault(self):
        manifest = self.backup()
        self.assertFalse(manifest["includes_keys"])
        with zipfile.ZipFile(self.zip) as z:
            names = set(z.namelist())
            self.assertIn("agents/a1/memory.md", names)
            self.assertIn("sessions.db", names)
            for absent in (".secret_key", "logs/backend.log", "vault/note.md", "sessions.db-wal"):
                self.assertNotIn(absent, names)
            self.assertIsNone(json.loads(z.read("model_endpoints.json"))["e1"]["api_key_encrypted"])
            self.assertIsNone(json.loads(z.read("integrations.json"))["i1"]["oauth"]["tokens"])
            copy = Path(tempfile.mkdtemp(dir=_DATA.name)) / "s.db"
            copy.write_bytes(z.read("sessions.db"))
        self.assertEqual(sqlite3.connect(copy).execute("SELECT id FROM sessions").fetchall(), [("s1",)],
                         "a consistent copy, written rows included")

    def test_keys_only_when_asked(self):
        self.backup(include_keys=True)
        with zipfile.ZipFile(self.zip) as z:
            self.assertIn(".secret_key", z.namelist())
            self.assertTrue(json.loads(z.read("model_endpoints.json"))["e1"]["api_key_encrypted"])

    def test_a_damaged_or_hostile_backup_is_refused(self):
        bad = Path(tempfile.mkdtemp(dir=_DATA.name))
        (bad / "not.zip").write_text("hello")
        with self.assertRaises(backup.BackupRefused):
            backup.check_backup(str(bad / "not.zip"))
        for name, manifest in (("nomanifest.zip", None), ("slip.zip", {"version": 1}), ("future.zip", {"version": 99})):
            with zipfile.ZipFile(bad / name, "w") as z:
                if manifest:
                    z.writestr(backup.MANIFEST, json.dumps(manifest))
                z.writestr("../escape.txt" if name == "slip.zip" else "a.txt", "x")
            with self.assertRaises(backup.BackupRefused, msg=name):
                backup.check_backup(str(bad / name))

    def restore_into(self, target: Path):
        (target / backup.RESTORE_DIR).mkdir(parents=True, exist_ok=True)
        os.replace(self.zip, target / backup.RESTORE_DIR / backup.PENDING)
        with patch("core.vault.resolve_vault_dir", return_value=str(target / "vault")):
            return backup.apply_pending_restore(str(target))

    def current(self) -> Path:
        target = Path(tempfile.mkdtemp(prefix="jarvis-restore-target-", dir=_DATA.name))
        (target / "vault").mkdir()
        (target / "vault" / "mine.md").write_text("vault stays")
        (target / ".secret_key").write_bytes(b"current-key")
        (target / "tasks.json").write_text('{"old": 1}')
        (target / "logs").mkdir()
        (target / "logs" / "backend.log").write_text("current log")
        return target

    def test_restore_swaps_at_start_and_keeps_a_safety_copy_and_the_vault(self):
        self.backup()
        target = self.current()
        result = self.restore_into(target)
        self.assertTrue(result["ok"])
        self.assertTrue((target / "agents" / "a1" / "memory.md").exists())
        self.assertFalse((target / "tasks.json").exists(), "the current data was replaced")
        self.assertEqual((Path(result["safety_copy"]) / "tasks.json").read_text(), '{"old": 1}')
        self.assertEqual((target / "vault" / "mine.md").read_text(), "vault stays")
        self.assertEqual((target / ".secret_key").read_bytes(), b"current-key", "a backup without keys keeps this key")
        self.assertEqual((target / "logs" / "backend.log").read_text(), "current log")
        self.assertFalse((target / backup.RESTORE_DIR / backup.PENDING).exists())
        self.assertTrue(json.loads((target / backup.RESTORE_DIR / backup.LAST).read_text())["ok"])
        self.assertIsNone(backup.apply_pending_restore(str(target)), "nothing pending, nothing done")

    def test_a_restore_that_fails_part_way_puts_everything_back(self):
        self.backup()
        target = self.current()
        calls = {"n": 0}
        real = zipfile.ZipFile.extract

        def flaky(zf, member, path=None, pwd=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real(zf, member, path, pwd)

        with patch.object(zipfile.ZipFile, "extract", flaky):
            result = self.restore_into(target)
        self.assertFalse(result["ok"])
        self.assertIn("disk full", result["error"])
        self.assertEqual((target / "tasks.json").read_text(), '{"old": 1}')
        self.assertFalse((target / "agents").exists(), "nothing of the backup is left behind")
        self.assertEqual((target / "vault" / "mine.md").read_text(), "vault stays")


class LegacyProfileTests(unittest.TestCase):
    def test_an_old_profile_upgrades_on_a_real_start(self):
        """The release check's upgrade stage, on the source backend."""
        profile = Path(tempfile.mkdtemp(prefix="jarvis-legacy-", dir=_DATA.name))
        data = profile / "data"
        subprocess.run([sys.executable, str(REPO / "scripts" / "legacy_profile.py"), "make", str(data)], check=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {**os.environ, "JARVIS_DATA_DIR": str(data), "JARVIS_VAULT_DIR": str(data / "vault"),
               "APP_PORT": str(port), "JARVIS_INSTANCE": "legacytest"}
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
                                  cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(60):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
                    break
                except OSError:
                    time.sleep(1)
            time.sleep(5)  # recovery and the first scheduler pass
        finally:
            subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"], capture_output=True)
            time.sleep(1)
        result = subprocess.run([sys.executable, str(REPO / "scripts" / "legacy_profile.py"), "check", str(data), "upgraded"],
                                capture_output=True, text=True)
        facts = json.loads(result.stdout)
        self.assertEqual(facts["problems"], [], facts)
        self.assertEqual(facts["schema_version"], store.SCHEMA_VERSION)


def tearDownModule():
    store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
