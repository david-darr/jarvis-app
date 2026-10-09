"""Offline publishing contracts. Every GitHub request uses a mock transport."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
import zipfile
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.tab_test_support import TemporaryDirectory

_DATA = TemporaryDirectory(dir=str(Path(__file__).resolve().parent))
os.environ["JARVIS_DATA_DIR"] = _DATA.name

import httpx
from fastapi import FastAPI
from core import custom_tabs, integrations, store_schema, tab_install
from core.atomic_io import write_json_atomic
from core.middleware import require_admin
from core.secret_storage import decrypt, encrypt
from routes import store_routes
from services import github_account as account, store_publish as publisher, skills_service, skill_curator
from services import task_service as task_module

TOKEN = "private-token-for-tests"
PR = {"number": 42, "html_url": "https://github.com/david-darr/kairos-store/pull/42"}


class PublishTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory(dir=_DATA.name); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1000
        self.calls = []
        self.responses = []
        self.items = []
        self.installs = {}
        self.tab = self.root / "tabs/focus"
        self.tab.mkdir(parents=True)
        self.tab_meta = dict(slug="focus", name="Focus", description="Focus notes", version="1.0.0", api=1, hooks=[])
        (self.tab / "tab.json").write_text(json.dumps(self.tab_meta), encoding="utf-8")
        (self.tab / "routes.py").write_text("from fastapi import APIRouter\nrouter = APIRouter()\n", encoding="utf-8")
        (self.tab / "view.js").write_text("export function render() {}\n", encoding="utf-8")
        self.skill = self.root / "skills/notes"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\ndescription: Review supplied notes\n---\nList decisions.\n", encoding="utf-8")
        (self.skill / "references").mkdir()
        (self.skill / "references/format.md").write_text("List owners when supplied.\n", encoding="utf-8")
        self.server = dict(id="server1", kind="mcp_server", name="Reference", mcp_type="http", url="https://example.com/mcp",
            auth="oauth", api_key_encrypted="do-not-export", headers={"Authorization": TOKEN}, oauth={"tokens": TOKEN})
        self.task = dict(id="task1", name="Review", prompt="Review supplied notes", schedule_kind="daily", run_time="06:00",
            deliver_to_channel="private-channel", endpoint_id=None, context="private-context", builtin_action=None)
        real_client = httpx.AsyncClient
        self.real_client = real_client
        def handler(request):
            self.calls.append((request.method, str(request.url), json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") else request.content.decode(), dict(request.headers)))
            if not self.responses:
                raise AssertionError("Unexpected GitHub call: " + str(request.url))
            status, body = self.responses.pop(0)
            return httpx.Response(status, json=body)
        for p in (
            patch.object(account, "FILE", self.root / "github.json"),
            patch.object(account, "client", side_effect=lambda: real_client(transport=httpx.MockTransport(handler), timeout=20, follow_redirects=False, trust_env=False)),
            patch.object(account, "_now", side_effect=lambda: self.now),
            patch.dict(os.environ, {"KAIROS_GITHUB_CLIENT_ID": "test-client-id"}),
            patch.object(custom_tabs, "USER_TABS_DIR", str(self.root / "tabs")),
            patch.object(skills_service, "SKILLS_DIR", str(self.root / "skills")),
            patch.object(skill_curator, "describe", return_value={"source": "imported"}),
            patch.object(integrations, "get_integration", side_effect=lambda ident: copy.deepcopy(self.server) if ident == "server1" else None),
            patch.object(task_module.task_service, "get_task", side_effect=lambda ident: copy.deepcopy(self.task) if ident == "task1" else None),
            patch.object(publisher.store_catalog, "snapshot", new=AsyncMock(side_effect=lambda **kw: {"index": {"items": self.items}})),
            patch.object(publisher.store_catalog, "installs", side_effect=lambda: self.installs),
        ):
            p.start(); self.addCleanup(p.stop)
        account._pending = None
        self.signed_in()

    def signed_in(self, login="alice"):
        write_json_atomic(str(account.FILE), {"account": encrypt(json.dumps({"login": login, "token": TOKEN}))})

    def respond(self, *bodies):
        self.responses.extend((200, b) for b in bodies)

    async def start_flow(self):
        account.FILE.unlink(missing_ok=True)
        self.respond(dict(device_code="server-only-device-code", user_code="ABCD-EFGH", verification_uri="https://github.com/login/device", expires_in=900, interval=5))
        result = await account.start()
        self.now += 5
        return result

    async def test_pending_sign_in_survives_a_redrawn_view(self):
        # The Tool Store view that started sign-in can be redrawn; status
        # must hand the waiting code to the new view (never the device code).
        await self.start_flow()
        pending = account.status()["pending"]
        self.assertEqual(pending["user_code"], "ABCD-EFGH")
        self.assertEqual(pending["verification_uri"], "https://github.com/login/device")
        self.assertNotIn("server-only-device-code", json.dumps(account.status()))
        self.respond({"access_token": TOKEN}, {"login": "alice"})
        await account.poll()
        self.assertNotIn("pending", account.status())

    async def test_device_happy_path_encrypted_and_safe(self):
        result = await self.start_flow()
        self.assertEqual(set(result), {"user_code", "verification_uri", "expires_in", "interval"})
        self.respond({"access_token": TOKEN}, {"login": "alice"})
        result = await account.poll()
        self.assertEqual(result, {"configured": True, "signed_in": True, "login": "alice", "state": "signed_in"})
        self.assertEqual(account.status(), {"configured": True, "signed_in": True, "login": "alice"})
        self.assertNotIn(TOKEN, account.FILE.read_text())
        saved = json.loads(account.FILE.read_text())
        self.assertEqual(json.loads(decrypt(saved["account"])), {"login": "alice", "token": TOKEN})
        self.assertEqual([c[:2] for c in self.calls], [("POST", account.DEVICE_URL), ("POST", account.TOKEN_URL), ("GET", "https://api.github.com/user")])
        self.assertIn("scope=public_repo", self.calls[0][2])
        self.assertIn("device_code=server-only-device-code", self.calls[1][2])
        self.assertNotIn("authorization", self.calls[0][3])
        self.assertEqual(self.calls[2][3]["authorization"], "Bearer " + TOKEN)
        await account.sign_out()
        self.assertFalse(account.FILE.exists())
        self.assertFalse(account.status()["signed_in"])

    async def test_pending_slow_down_and_interval_enforced(self):
        await self.start_flow()
        self.respond({"error": "authorization_pending"}, {"error": "slow_down"})
        self.assertEqual(await account.poll(), {"state": "authorization_pending", "interval": 5})
        self.assertEqual(await account.poll(), {"state": "authorization_pending", "interval": 5})
        self.assertEqual(len(self.calls), 2)
        self.now += 5
        self.assertEqual(await account.poll(), {"state": "slow_down", "interval": 10})
        self.now += 9
        await account.poll()
        self.assertEqual(len(self.calls), 3)

    async def test_expired_and_denied(self):
        for error, message in (("expired_token", "expired"), ("access_denied", "denied")):
            await self.start_flow()
            self.respond({"error": error})
            with self.assertRaisesRegex(ValueError, message):
                await account.poll()
            self.assertIsNone(account._pending)
            self.assertFalse(account.status()["signed_in"])
        await self.start_flow()
        self.now += 901
        with self.assertRaisesRegex(ValueError, "expired"):
            await account.poll()

    async def test_sign_out_cancels_pending(self):
        await self.start_flow()
        await account.sign_out()
        with self.assertRaisesRegex(ValueError, "expired"):
            await account.poll()

    async def test_unconfigured(self):
        with patch.dict(os.environ, {"KAIROS_GITHUB_CLIENT_ID": ""}):
            self.assertFalse(account.status()["configured"])
            with self.assertRaisesRegex(ValueError, "GitHub sign-in isn't set up in this build yet"):
                await account.start()
        self.assertFalse(self.calls)

    async def test_allowlist_no_redirect_size_and_token_scope(self):
        for url in ("http://github.com/login/device/code", "https://github.com/other", "https://api.github.com.evil.com/user", "https://user@api.github.com/user", "https://api.github.com/user?secret=x"):
            with self.assertRaisesRegex(ValueError, "allowlisted"):
                await account.request("GET", url)
        with self.assertRaisesRegex(ValueError, "restricted"):
            await account.api("GET", "/repos/alice/other/pulls")
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            await account.request("POST", account.TOKEN_URL, token=TOKEN)
        self.responses.append((302, {"redirect": "evil"}))
        with self.assertRaises(account.GitHubError):
            await account.request("GET", "https://api.github.com/user")
        self.respond({"padding": "x" * (2 * 1024 * 1024)})
        with self.assertRaisesRegex(ValueError, "size limit"):
            await account.request("GET", "https://api.github.com/user")

    async def test_exports_and_tab_round_trip(self):
        exported = publisher.export("tab", "focus")
        original = tab_install.export("focus")
        with zipfile.ZipFile(io.BytesIO(original)) as archive:
            self.assertEqual(exported["files"], {n[len("focus/"):]: archive.read(n) for n in archive.namelist()})
        round_trip = store_schema.tab_archive("focus", exported["files"])
        stage = self.root / "roundtrip"; stage.mkdir()
        folder = tab_install.unpack(round_trip, stage)
        self.assertEqual({p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}, exported["files"])
        skill = publisher.export("skill", "notes")
        self.assertEqual(set(skill["files"]), {"SKILL.md", "references/format.md"})
        tool = json.loads(publisher.export("tool", "server1")["files"]["server.json"])
        self.assertEqual(tool, dict(name="Reference", url="https://example.com/mcp", transport="http", auth_type="oauth"))
        self.server["auth"] = None
        self.assertEqual(json.loads(publisher.export("tool", "server1")["files"]["server.json"])["auth_type"], "none")
        task = json.loads(publisher.export("automation", "task1")["files"]["automation.json"])
        self.assertEqual(task, dict(title="Review", prompt="Review supplied notes", schedule=dict(schedule_kind="daily", run_time="06:00")))
        for kind, local in (("tab", "focus"), ("skill", "notes"), ("tool", "server1"), ("automation", "task1")):
            preview = await publisher.prepare(kind, local)
            files = {p.split("/", 3)[3]: v.encode() for p, v in preview["files"].items()}
            store_schema.validate_bytes(preview["manifest"], files)
            self.assertEqual(preview["preview_hash"], store_schema.archive_sha256(files))
            self.assertNotIn(TOKEN, json.dumps(preview))

    async def test_tab_slug_edit_keeps_schema_inverse(self):
        preview = await publisher.prepare("tab", "focus", "new_focus", "New focus", "New notes", "2.0.0")
        self.assertEqual(json.loads(preview["files"]["items/tab/new_focus/tab.json"])["slug"], "new_focus")
        self.assertEqual(preview["manifest"]["slug"], "new_focus")

    async def test_secrets_refused_in_every_kind_with_filename(self):
        secret = "ghp_" + "a" * 36
        (self.tab / "routes.py").write_text("# " + secret, encoding="utf-8")
        (self.skill / "references/format.md").write_text(secret, encoding="utf-8")
        self.server["url"] += "/" + secret
        self.task["prompt"] += " " + secret
        for kind, local, filename in (("tab", "focus", "routes.py"), ("skill", "notes", "references/format.md"), ("tool", "server1", "server.json"), ("automation", "task1", "automation.json")):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "Possible secret in " + filename):
                await publisher.prepare(kind, local)
        (self.skill / ".env").write_text("PRIVATE=secret", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"Unsafe file path: .env"):
            publisher.export("skill", "notes")

    async def test_stdio_and_builtins_refused(self):
        self.server["mcp_type"] = "stdio"
        with self.assertRaisesRegex(ValueError, "Local command"):
            publisher.export("tool", "server1")
        with patch.object(skill_curator, "describe", return_value={"source": "bundled"}), self.assertRaisesRegex(ValueError, "Built-in"):
            publisher.export("skill", "notes")
        with self.assertRaisesRegex(ValueError, "Only folder user tabs"):
            publisher.export("tab", "crm")
        self.task["builtin_action"] = "daily_brief"
        with self.assertRaisesRegex(ValueError, "built-in"):
            publisher.export("automation", "task1")
        self.task["builtin_action"] = None; self.task["schedule_kind"] = "card"
        with self.assertRaisesRegex(ValueError, "not cards"):
            publisher.export("automation", "task1")

    async def test_schedule_rules_no_silent_conversion(self):
        self.task.update(schedule_kind="once", run_at="2000-01-01T00:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "in the past"):
            publisher.export("automation", "task1")
        self.task["run_at"] = "2999-01-01T00:00:00+00:00"
        self.assertEqual(json.loads(publisher.export("automation", "task1")["files"]["automation.json"])["schedule"], {"schedule_kind": "once", "run_at": self.task["run_at"]})
        self.task.update(schedule_kind="interval", interval_seconds=60, model_hint="Model preference")
        preview = await publisher.prepare("automation", "task1")
        self.assertEqual(json.loads(next(v for p, v in preview["files"].items() if p.endswith("automation.json")))["model_hint"], "Model preference")
        self.task["interval_seconds"] = 0
        with self.assertRaisesRegex(ValueError, "interval_seconds"):
            publisher.export("automation", "task1")
        self.task.update(schedule_kind="daily", run_time="24:00")
        with self.assertRaisesRegex(ValueError, "run_time"):
            publisher.export("automation", "task1")

    async def test_created_and_imported_skill_sources(self):
        for source in ("user", "imported", "recorded", "unknown"):
            with self.subTest(source=source), patch.object(skill_curator, "describe", return_value={"source": source}):
                self.assertIn("SKILL.md", publisher.export("skill", "notes")["files"])

    async def test_account_switch_stops_store_token_use(self):
        with self.assertRaisesRegex(ValueError, "account changed"):
            await account.api("GET", "/repos/david-darr/kairos-store/pulls", expected_login="bob")
        self.assertFalse(self.calls)

    async def test_account_switch_during_export_refuses_preview(self):
        original = publisher.export
        def switch(kind, local_id):
            result = original(kind, local_id)
            self.signed_in("bob")
            return result
        with patch.object(publisher, "export", side_effect=switch), self.assertRaisesRegex(ValueError, "account changed"):
            await publisher.prepare("tab", "focus")
        publisher.store_catalog.snapshot.assert_not_awaited()

    async def test_installed_author_all_kinds(self):
        for kind, local in (("tab", "focus"), ("skill", "notes"), ("tool", "server1"), ("automation", "task1")):
            self.installs = {kind + "/original": dict(kind=kind, slug="original", local_id=local, author="bob")}
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "original author"):
                publisher.export(kind, local)
            self.installs[kind + "/original"]["author"] = "ALICE"
            self.assertEqual(publisher.export(kind, local)["slug"], "original")

    async def test_catalog_ownership_version_and_defaults(self):
        self.items = [dict(kind="tab", slug="focus", author="bob", version="1.2.3", files=["tab.json", "routes.py", "view.js"])]
        with self.assertRaisesRegex(ValueError, "another author"):
            await publisher.prepare("tab", "focus")
        defaults = await publisher.suggestions("tab", "focus")
        self.assertEqual(defaults["version"], "1.0.0")  # form remains editable to pick another slug
        await publisher.prepare("tab", "focus", slug="another_slug")
        self.items[0]["author"] = "alice"
        for version in ("1.2.3", "1.2.2", "1.2.3+build", "1.2.3-beta"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "higher semver"):
                await publisher.prepare("tab", "focus", version=version)
        self.assertEqual((await publisher.suggestions("tab", "focus"))["version"], "1.2.4")
        self.assertTrue((await publisher.prepare("tab", "focus", version="1.2.4"))["update"])
        for slug in ("Bad", "bad-slug", "routes", "../escape", "con", "x" * 65):
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                await publisher.prepare("tab", "focus", slug=slug)
        with patch.object(publisher.store_catalog, "snapshot", new=AsyncMock(return_value={"stale": True, "index": {"items": []}})), self.assertRaisesRegex(ValueError, "live store catalog"):
            await publisher.prepare("tab", "focus")

    async def publish_args(self, kind="tool", local="server1"):
        preview = await publisher.prepare(kind, local)
        return {"kind": kind, "local_id": local, **{k: preview["manifest"][k] for k in ("slug", "name", "description", "version")}, "preview_hash": preview["preview_hash"], "confirmed_public": True}, preview

    async def test_confirmation_and_hash_refuse_before_http(self):
        args, preview = await self.publish_args()
        for confirmation in (False, 1, "true", None):
            with self.assertRaisesRegex(ValueError, "Confirm"):
                await publisher.publish(**{**args, "confirmed_public": confirmation})
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            await publisher.publish(**{**args, "preview_hash": "wrong"})
        self.server["url"] = "https://example.com/changed"
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            await publisher.publish(**args)
        self.assertFalse(self.calls)

    def upload_responses(self, preview, pr_response=PR, ref_status=201):
        self.responses.extend([(202, {}), (200, {"fork": True, "parent": {"full_name": "david-darr/kairos-store"}}),
            (200, {}), (200, {"sha": "upstream-main", "commit": {"tree": {"sha": "upstream-tree"}}})])
        self.responses.extend((201, {"sha": "blob-" + str(i)}) for i in range(len(preview["files"])))
        self.responses.extend([(201, {"sha": "new-tree"}), (201, {"sha": "new-commit"}), (ref_status, {}), (201, pr_response)])

    async def test_store_owner_branches_on_the_store_instead_of_forking(self):
        # GitHub refuses to fork a repository into its own owner's account.
        self.signed_in("david-darr")
        args, preview = await self.publish_args()
        self.responses.append((200, {"sha": "upstream-main", "commit": {"tree": {"sha": "upstream-tree"}}}))
        self.responses.extend((201, {"sha": "blob-" + str(i)}) for i in range(len(preview["files"])))
        self.responses.extend([(201, {"sha": "new-tree"}), (201, {"sha": "new-commit"}), (201, {}), (201, PR)])
        self.assertEqual(await publisher.publish(**args), {"url": PR["html_url"], "number": 42})
        upstream = "https://api.github.com/repos/david-darr/kairos-store"
        self.assertEqual([c[:2] for c in self.calls], [("GET", upstream + "/commits/main"),
            *[("POST", upstream + "/git/blobs")] * len(preview["files"]), ("POST", upstream + "/git/trees"),
            ("POST", upstream + "/git/commits"), ("POST", upstream + "/git/refs"), ("POST", upstream + "/pulls")])
        self.assertEqual(self.calls[-1][2]["head"], "kairos/tool-reference-1.0.0")

    async def test_publish_exact_order_payloads_and_preview(self):
        args, preview = await self.publish_args()
        self.upload_responses(preview)
        result = await publisher.publish(**args)
        self.assertEqual(result, {"url": PR["html_url"], "number": 42})
        fork = "https://api.github.com/repos/alice/kairos-store"
        upstream = "https://api.github.com/repos/david-darr/kairos-store"
        self.assertEqual([c[:2] for c in self.calls], [("POST", upstream + "/forks"), ("GET", fork), ("POST", fork + "/merge-upstream"), ("GET", upstream + "/commits/main"),
            *[("POST", fork + "/git/blobs")] * len(preview["files"]), ("POST", fork + "/git/trees"), ("POST", fork + "/git/commits"), ("POST", fork + "/git/refs"), ("POST", upstream + "/pulls")])
        self.assertTrue(all(c[3]["authorization"] == "Bearer " + TOKEN for c in self.calls))
        self.assertEqual(self.calls[2][2], {"branch": "main"})
        for i, (path, text) in enumerate(preview["files"].items()):
            blob = self.calls[4 + i][2]
            self.assertEqual(blob["encoding"], "base64")
            self.assertEqual(base64.b64decode(blob["content"]).decode(), text)
        tree, commit, ref, pull = [c[2] for c in self.calls[-4:]]
        self.assertEqual(tree["base_tree"], "upstream-tree")
        self.assertEqual([t["path"] for t in tree["tree"]], list(preview["files"]))
        self.assertTrue(all(t["mode"] == "100644" and t["type"] == "blob" for t in tree["tree"]))
        self.assertEqual(commit, dict(message="Add tool: Reference 1.0.0", tree="new-tree", parents=["upstream-main"]))
        self.assertEqual(ref, dict(ref="refs/heads/kairos/tool-reference-1.0.0", sha="new-commit"))
        self.assertEqual(pull["head"], "alice:kairos/tool-reference-1.0.0")
        self.assertEqual(pull["base"], "main")
        self.assertEqual(pull["title"], "Add tool: Reference 1.0.0")
        self.assertIn("Submitted from Kairos", pull["body"]); self.assertIn("MIT licence", pull["body"])
        for path in preview["files"]:
            self.assertIn(path, pull["body"])
        self.assertFalse(self.responses)

    async def test_update_title_and_removed_files(self):
        self.items = [dict(kind="skill", slug="notes", author="alice", version="1.0.0", files=["SKILL.md", "old.md"])]
        args, preview = await self.publish_args("skill", "notes")
        self.upload_responses(preview)
        await publisher.publish(**args)
        self.assertEqual(self.calls[-1][2]["title"], "Update skill: Notes 1.0.1")
        self.assertIn({"path": "items/skill/notes/old.md", "mode": "100644", "type": "blob", "sha": None}, self.calls[-4][2]["tree"])
        self.assertIn("items/skill/notes/old.md", self.calls[-1][2]["body"])

    async def test_fork_poll_and_nonfork_refusal(self):
        args, preview = await self.publish_args()
        self.upload_responses(preview)
        self.responses.insert(1, (404, {}))
        with patch.object(publisher.asyncio, "sleep", new=AsyncMock()) as sleep:
            await publisher.publish(**args)
            sleep.assert_awaited_once_with(2)
        self.calls.clear()
        self.responses.extend([(202, {}), (200, {"fork": False})])
        with self.assertRaisesRegex(ValueError, "not a fork"):
            await publisher.publish(**args)
        self.assertEqual(len(self.calls), 2)

    async def test_existing_pr_returned_for_ref_or_pull_422(self):
        for conflict in ("ref", "pull"):
            self.calls.clear()
            args, preview = await self.publish_args()
            self.upload_responses(preview)
            if conflict == "ref":
                self.responses[-2:] = [(422, {}), (200, [PR])]
            else:
                self.responses[-1:] = [(422, {}), (200, [PR])]
            self.assertEqual(await publisher.publish(**args), {"url": PR["html_url"], "number": 42})
            self.assertIn("head=alice%3Akairos%2Ftool-reference-1.0.0", self.calls[-1][1])
            self.assertIn("base=main", self.calls[-1][1])

    async def test_clear_403_and_422_errors(self):
        args, preview = await self.publish_args()
        self.responses.append((403, {}))
        with self.assertRaisesRegex(ValueError, "public_repo"):
            await publisher.publish(**args)
        self.upload_responses(preview)
        self.responses[-1:] = [(422, {}), (200, [])]
        with self.assertRaisesRegex(ValueError, "may already exist"):
            await publisher.publish(**args)

    async def test_submissions_mapping_pagination_and_cap(self):
        def pr(number, login, state="open", merged_at=None):
            return {"number": number, "html_url": f"https://github.com/david-darr/kairos-store/pull/{number}", "user": {"login": login}, "title": str(number), "state": state, "merged_at": merged_at, "updated_at": "2026-10-08"}
        self.respond([pr(1, "ALICE"), pr(2, "alice", "closed", "2026-10-08"), pr(3, "alice", "closed"), pr(4, "bob")])
        mapped = await publisher.submissions()
        self.assertEqual([p["state"] for p in mapped], ["open", "merged", "closed"])
        self.assertTrue(all(set(p) == {"number", "title", "state", "url", "updated_at"} for p in mapped))
        self.calls.clear()
        self.responses.extend((200, [pr(i + 1, "bob") for i in range(100)]) for _ in range(10))
        self.assertEqual(await publisher.submissions(), [])
        self.assertEqual(len(self.calls), 10)
        self.assertIn("page=10", self.calls[-1][1])

    async def test_routes_admin_only_and_credentials_never_returned(self):
        app = FastAPI(); app.include_router(store_routes.router)
        requests = [("GET", "/api/store/github", None), ("POST", "/api/store/github/start", {}), ("POST", "/api/store/github/poll", {}),
            ("POST", "/api/store/github/sign-out", {}), ("POST", "/api/store/publish/export", {"kind": "tab", "local_id": "focus"}),
            ("POST", "/api/store/publish/prepare", {"kind": "tab", "local_id": "focus"}), ("POST", "/api/store/publish", {}), ("GET", "/api/store/submissions", None)]
        async with self.real_client(transport=httpx.ASGITransport(app=app), base_url="https://kairos.test") as connection:
            for current_user, admin, status in ((None, False, 401), ("member", False, 403)):
                with patch("core.middleware.get_current_user", return_value=current_user), patch("core.middleware.auth_manager.is_admin", return_value=admin):
                    for method, path, body in requests:
                        response = await connection.request(method, path, json=body)
                        self.assertEqual(response.status_code, status, path)
            app.dependency_overrides[require_admin] = lambda: "admin"
            response = await connection.get("/api/store/github")
            self.assertEqual(response.json(), {"configured": True, "signed_in": True, "login": "alice"})
            self.assertNotIn(TOKEN, response.text)
            await self.start_flow()
            self.respond(dict(device_code="server-only-device-code", user_code="ABCD-EFGH", verification_uri="https://github.com/login/device", expires_in=900, interval=5))
            response = await connection.post("/api/store/github/start")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("device_code", response.text)
            self.now += 5; self.respond({"access_token": TOKEN}, {"login": "alice"})
            response = await connection.post("/api/store/github/poll")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(TOKEN, response.text); self.assertNotIn("device_code", response.text)
            args, preview = await self.publish_args()
            response = await connection.post("/api/store/publish", json={**args, "confirmed_public": "true"})
            self.assertEqual(response.status_code, 422)
            response = await connection.post("/api/store/publish/prepare", json={"kind": "tool", "local_id": "server1"})
            self.assertEqual(response.json(), preview)
            self.respond([])
            self.assertEqual((await connection.get("/api/store/submissions")).json(), [])
            response = await connection.post("/api/store/github/sign-out")
            self.assertFalse(response.json()["signed_in"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
