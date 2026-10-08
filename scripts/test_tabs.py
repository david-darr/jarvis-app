"""Folder/legacy tab contracts with real FastAPI apps and TestClient.
No external server, subprocesses or real user data.
Run: python scripts/test_tabs.py
"""
import asyncio
import time
import importlib
import json
import os
import shutil
import sys
import tempfile
import io
import base64
import stat
import zipfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.tab_test_support import TemporaryDirectory

_DATA = TemporaryDirectory(prefix=".tab-test-", dir=str(Path(__file__).resolve().parent))
os.environ["JARVIS_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from core import custom_tabs as tabs, settings, tab_folders, tab_api, tab_hooks
from core.middleware import require_user, require_admin
from routes import system_routes

REPO = Path(__file__).resolve().parents[1]
ROUTES = '''from fastapi import APIRouter
from . import service
router = APIRouter(prefix="/fixture/{slug}")
@router.get("")
def answer(): return {{"answer": service.answer}}
'''


class TabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(dir=_DATA.name)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.users = self.root / "data" / "tabs"
        self.prebuilt = self.root / "tabs"
        self.routes = self.users / "routes"
        self.services = self.users / "services"
        self.views = self.users / "views"
        self.prebuilt.mkdir(parents=True)
        for directory in (self.routes, self.services, self.views, self.root / "routes"):
            directory.mkdir(parents=True)
        self.saved = {}
        for name, value in dict(USER_TABS_DIR=str(self.users), USER_ROUTES_DIR=str(self.routes),
                USER_SERVICES_DIR=str(self.services), USER_VIEWS_DIR=str(self.views),
                USER_TAB_CODE_DIRS=tuple(map(str, (self.routes, self.services, self.views))),
                PREBUILT_TABS_DIR=str(self.prebuilt), ROUTES_DIR=str(self.root / "routes"),
                _paths_extended=False).items():
            p = patch.object(tabs, name, value)
            p.start(); self.addCleanup(p.stop)
        for p in (patch.object(settings, "get_setting", side_effect=lambda key: self.saved.get(key)),
                  patch.object(settings, "update_settings", side_effect=lambda **fields: self.saved.update(fields) or self.saved)):
            p.start(); self.addCleanup(p.stop)
        import routes, services
        old_paths = (list(routes.__path__), list(services.__path__))
        self.addCleanup(lambda: setattr(routes, "__path__", old_paths[0]))
        self.addCleanup(lambda: setattr(services, "__path__", old_paths[1]))
        self.addCleanup(self.clear_modules)
        self.addCleanup(lambda: asyncio.run(tab_hooks.stop_all()))
        self.clear_modules()
        self.app = FastAPI()
        self.app.include_router(system_routes.router)
        self.app.include_router(system_routes.custom_views_router)
        self.app.dependency_overrides[require_user] = lambda: "reviewer"
        self.app.dependency_overrides[require_admin] = lambda: "reviewer"
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def clear_modules(self):
        for slug in list(tab_folders._registered):
            tab_folders.unregister(slug)
        for name in list(sys.modules):
            if name in ("routes.tab_jobs", "routes.tab_minecraft", "routes.tab_school",
                        "services.jobs_service", "services.minecraft_service"):
                del sys.modules[name]

    def folder(self, folder_slug="alpha", *, user=True, answer=42, **meta):
        slug = folder_slug
        folder = (self.users if user else self.prebuilt) / slug
        folder.mkdir()
        manifest = dict(slug=slug, name=slug.title(), version="1.0.0", description="Fixture tab",
                        api=1, hooks=[], icon_svg="<svg/>", blurb="A blurb", detail="Some detail")
        manifest.update(meta)
        (folder / "tab.json").write_text(json.dumps(manifest), encoding="utf-8")
        (folder / "routes.py").write_text(ROUTES.format(slug=slug), encoding="utf-8")
        (folder / "service.py").write_text(f"answer = {answer}\n", encoding="utf-8")
        (folder / "view.js").write_text("export function render(container) {}\n", encoding="utf-8")
        (folder / "view.css").write_text(".fixture { color: blue; }\n", encoding="utf-8")
        return folder

    def approve(self, slug):
        pending = next(e for e in tabs.pending_approvals() if e["id"] == slug)
        return tabs.approve_user_tab(slug, pending["fingerprint"])

    def status(self, slug, kind="user"):
        return next(e for e in tabs.list_tabs() if e["slug"] == slug and e["kind"] == kind)

    def test_folder_mounts_and_serves_views_and_styles(self):
        self.folder()
        self.approve("alpha")
        tabs.mount_all(self.app)
        self.assertEqual(self.client.get("/fixture/alpha").json(), {"answer": 42})
        manifest = next(m for m in tabs.list_manifests() if m["id"] == "alpha")
        self.assertEqual(manifest, dict(id="alpha", label="Alpha", icon_svg="<svg/>",
            view_url="/tab-files/alpha/view.js", style_url="/tab-files/alpha/view.css", user_tab=True, format="folder"))
        for filename, mime in (("view.js", "application/javascript"), ("view.css", "text/css")):
            response = self.client.get(f"/tab-files/alpha/{filename}")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith(mime))
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.status("alpha")["status"], "on")
        self.assertFalse((self.users / "alpha" / "__pycache__").exists())

    def test_relative_imports_and_identically_named_modules_are_isolated(self):
        for slug, answer in (("alpha", 1), ("beta", 2)):
            self.folder(slug, answer=answer)
            self.approve(slug)
        tabs.mount_all(self.app)
        self.assertEqual(self.client.get("/fixture/alpha").json()["answer"], 1)
        self.assertEqual(self.client.get("/fixture/beta").json()["answer"], 2)
        self.assertIsNot(sys.modules["kairos_tabs.alpha.service"], sys.modules["kairos_tabs.beta.service"])
        self.assertEqual(sys.modules["kairos_tabs.alpha"].__path__, [str(self.users / "alpha")])

    def test_mounted_folder_blocks_edits_and_requires_restart_after_approval(self):
        folder = self.folder()
        first = self.approve("alpha")
        tabs.mount_all(self.app)
        mounted = self.app.state.custom_tab_modules["alpha"]
        routes = list(self.app.state.custom_tab_routes["alpha"])
        self.assertEqual(self.client.get("/fixture/alpha").json(), {"answer": 42})
        (folder / "service.py").write_text("answer = 17\n", encoding="utf-8")
        blocked = self.client.get("/fixture/alpha")
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"],
            "This tab was removed or changed. Restart Kairos to load the current version.")
        current = next(e for e in tabs.pending_approvals() if e["id"] == "alpha")
        self.assertNotEqual(first["fingerprint"], current["fingerprint"])
        approved = self.client.post("/api/system/custom-tabs/alpha/approve",
            json={"fingerprint": current["fingerprint"]})
        self.assertEqual(approved.status_code, 200)
        self.assertTrue(approved.json()["restart_required"])
        self.assertFalse(tabs.mount_one(self.app, "alpha"))
        self.assertIs(self.app.state.custom_tab_modules["alpha"], mounted)
        self.assertEqual(self.app.state.custom_tab_routes["alpha"], routes)
        self.assertEqual(self.client.get("/fixture/alpha").status_code, 409)
        restarted = FastAPI()
        tabs.mount_all(restarted)
        with TestClient(restarted) as client:
            self.assertEqual(client.get("/fixture/alpha").json(), {"answer": 17})

    def test_mounted_user_routes_block_removal_for_both_formats(self):
        self.folder()
        self.legacy()
        for slug, url in (("alpha", "/fixture/alpha"), ("jobs", "/legacy/jobs")):
            with self.subTest(format=slug):
                self.approve(slug)
                tabs.mount_all(self.app)
                self.assertEqual(self.client.get(url).status_code, 200)
                removed = self.client.delete(f"/api/system/custom-tabs/{slug}")
                self.assertEqual(removed.status_code, 200)
                self.assertTrue(removed.json()["restart_required"])
                response = self.client.get(url)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"],
                    "This tab was removed or changed. Restart Kairos to load the current version.")

    def test_mounted_legacy_approval_stays_bound_to_loaded_tree(self):
        self.legacy()
        self.approve("jobs")
        tabs.mount_all(self.app)
        loaded = self.app.state.custom_tab_entries["jobs"]["fingerprint"]
        (self.services / "jobs_service.py").write_text("answer = 17\n", encoding="utf-8")
        self.assertEqual(self.client.get("/legacy/jobs").status_code, 409)
        current = next(e for e in tabs.pending_approvals() if e["id"] == "jobs")
        approved = self.client.post("/api/system/custom-tabs/jobs/approve",
            json={"fingerprint": current["fingerprint"]})
        self.assertEqual(approved.status_code, 200)
        self.assertTrue(approved.json()["restart_required"])
        # Discovery must not attach the new approval to cached old Python code.
        tabs.discover()
        self.assertEqual(sys.modules["routes.tab_jobs"]._jarvis_tab_entry["fingerprint"], loaded)
        self.assertFalse(tabs.mount_one(self.app, "jobs"))
        self.assertEqual(self.client.get("/legacy/jobs").status_code, 409)

    def test_prebuilt_off_and_on_reuses_unchanged_mounted_routes(self):
        self.folder(user=False)
        enabled = self.client.post("/api/system/tab-templates/alpha", json={"enabled": True})
        self.assertEqual(enabled.status_code, 200)
        self.assertFalse(enabled.json()["restart_required"])
        mounted = self.app.state.custom_tab_modules["alpha"]
        routes = list(self.app.state.custom_tab_routes["alpha"])
        self.assertEqual(self.client.get("/fixture/alpha").json(), {"answer": 42})
        disabled = self.client.post("/api/system/tab-templates/alpha", json={"enabled": False})
        self.assertEqual(disabled.status_code, 200)
        self.assertTrue(disabled.json()["restart_required"])
        response = self.client.get("/fixture/alpha")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "This tab is off.")
        enabled = self.client.post("/api/system/tab-templates/alpha", json={"enabled": True})
        self.assertEqual(enabled.status_code, 200)
        self.assertFalse(enabled.json()["restart_required"])
        self.assertIs(self.app.state.custom_tab_modules["alpha"], mounted)
        self.assertEqual(self.app.state.custom_tab_routes["alpha"], routes)
        self.assertEqual(self.client.get("/fixture/alpha").json(), {"answer": 42})

    def test_changed_prebuilt_cannot_remount_after_switching_back_on(self):
        folder = self.folder(user=False)
        self.client.post("/api/system/tab-templates/alpha", json={"enabled": True}).raise_for_status()
        self.client.post("/api/system/tab-templates/alpha", json={"enabled": False}).raise_for_status()
        (folder / "service.py").write_text("answer = 17\n", encoding="utf-8")
        enabled = self.client.post("/api/system/tab-templates/alpha", json={"enabled": True})
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(enabled.json()["restart_required"])
        self.assertEqual(self.client.get("/fixture/alpha").status_code, 409)

    def test_unchanged_approved_routes_keep_working_and_hash_only_their_folder(self):
        self.folder()
        self.folder("beta")
        self.legacy()
        for slug in ("alpha", "beta", "jobs"):
            self.approve(slug)
        tabs.mount_all(self.app)
        with patch.object(tab_folders, "fingerprint", wraps=tab_folders.fingerprint) as fingerprint:
            for _ in range(2):
                self.assertEqual(self.client.get("/fixture/alpha").json(), {"answer": 42})
            self.assertEqual(fingerprint.call_count, 2)
            self.assertTrue(all(call.args[0] == str(self.users / "alpha") for call in fingerprint.call_args_list))
        for _ in range(2):
            self.assertEqual(self.client.get("/legacy/jobs").json(), {"answer": 99})
        current = next(e for e in tabs.pending_approvals() if e["id"] == "alpha")
        approved = self.client.post("/api/system/custom-tabs/alpha/approve",
            json={"fingerprint": current["fingerprint"]})
        self.assertEqual(approved.status_code, 200)
        self.assertFalse(approved.json()["restart_required"])
        self.assertEqual(self.client.get("/fixture/alpha").json(), {"answer": 42})

    def test_guard_detects_same_size_edits_with_restored_timestamp(self):
        folder = self.folder()
        self.approve("alpha")
        tabs.mount_all(self.app)
        service = folder / "service.py"
        original = service.stat()
        service.write_text("answer = 17\n", encoding="utf-8")
        os.utime(service, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(service.stat().st_size, original.st_size)
        self.assertEqual(service.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertEqual(self.client.get("/fixture/alpha").status_code, 409)

    def test_revoked_approval_and_user_override_block_already_mounted_routes(self):
        self.folder()
        self.approve("alpha")
        self.folder("beta", user=False)
        tabs.set_template_enabled("beta", True)
        tabs.mount_all(self.app)
        self.saved[tabs._FOLDER_APPROVALS_KEY].pop("alpha")
        self.assertEqual(self.client.get("/fixture/alpha").status_code, 409)
        self.folder("beta", answer=17)
        self.assertEqual(self.client.get("/fixture/beta").status_code, 409)
        self.approve("beta")
        self.assertFalse(tabs.mount_one(self.app, "beta"))
        self.assertEqual(self.client.get("/fixture/beta").status_code, 409)

    def test_removing_unmounted_source_does_not_require_restart(self):
        self.folder()
        removed = self.client.delete("/api/system/custom-tabs/alpha")
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(removed.json()["restart_required"])
        self.folder("beta", user=False)
        disabled = self.client.post("/api/system/tab-templates/beta", json={"enabled": False})
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["restart_required"])

    def test_unapproved_tab_cannot_execute_mount_serve_or_be_imported(self):
        folder = self.folder("beta")
        marker = self.root / "executed"
        (folder / "routes.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
        self.folder("alpha")
        (self.users / "alpha" / "routes.py").write_text("import kairos_tabs.beta.service\n", encoding="utf-8")
        self.approve("alpha")
        tabs.mount_all(self.app)
        self.assertFalse(marker.exists())
        self.assertEqual(self.status("beta")["status"], "needs_approval")
        self.assertEqual(self.status("alpha")["status"], "failed")
        self.assertNotIn("kairos_tabs.beta", sys.modules)
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("kairos_tabs.beta.service")
        self.assertEqual(self.client.get("/fixture/beta").status_code, 404)
        self.assertEqual(self.client.get("/tab-files/beta/view.js").status_code, 404)

    def test_approval_and_edits_are_per_folder(self):
        alpha = self.folder()
        self.folder("beta")
        first = self.approve("alpha")
        self.assertFalse(tabs.user_tab_is_approved("beta"))
        self.approve("beta")
        tabs.discover()
        (alpha / "service.py").write_text("answer = 7\n", encoding="utf-8")
        self.assertFalse(tabs.user_tab_is_approved("alpha"))
        self.assertTrue(tabs.user_tab_is_approved("beta"))
        tabs.discover()
        self.assertNotIn("kairos_tabs.alpha", sys.modules)
        self.assertIn("kairos_tabs.beta", sys.modules)
        with self.assertRaisesRegex(ValueError, "source changed"):
            tabs.approve_user_tab("alpha", first["fingerprint"])
        self.assertIsNone(tabs.folder_file_bytes("alpha", "view.js"))

    def test_manifest_validation(self):
        for name, change in (("badslug", {"slug": "Wrong-Slug"}), ("mismatch", {"slug": "other"}),
                ("boolapi", {"api": True}), ("floatapi", {"api": 1.0}), ("badname", {"name": 42}),
                ("badhooks", {"hooks": "sync"})):
            with self.subTest(name=name):
                self.folder(name, **change)
                entry = self.status(name)
                self.assertEqual(entry["status"], "invalid")
                self.assertTrue(entry["reason"])
                self.assertNotIn(f"kairos_tabs.{name}", sys.modules)
        folder = self.folder("broken")
        (folder / "tab.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(self.status("broken")["status"], "invalid")

    def test_unknown_metadata_is_ignored_and_missing_routes_is_invalid(self):
        folder = self.folder(extra={"future": True})
        self.approve("alpha")
        self.assertEqual(self.status("alpha")["status"], "on")
        self.assertNotIn("extra", tab_folders.metadata(str(folder)))
        (folder / "routes.py").unlink()
        self.assertEqual(self.status("alpha")["status"], "invalid")

    def test_newer_api_is_not_imported_even_if_enabled_or_hash_approved(self):
        folder = self.folder(api=2)
        self.saved[tabs._FOLDER_APPROVALS_KEY] = {"alpha": tab_folders.fingerprint(str(folder))[0]}
        self.folder("future", user=False, api=2)
        self.saved["enabled_tab_templates"] = ["future"]
        self.assertEqual(self.status("alpha")["status"], "needs_newer_kairos")
        self.assertEqual(self.status("future", "prebuilt")["status"], "needs_newer_kairos")
        self.assertNotIn("kairos_tabs.alpha", sys.modules)
        self.assertIsNone(tabs.folder_file_bytes("alpha", "view.js"))
        with self.assertRaises(ValueError):
            self.approve("alpha")

    def legacy(self, slug="jobs"):
        # Copy split-layout fixtures unchanged: their absolute helper import
        # is the compatibility contract, not a rewritten folder-format tab.
        fixture = self.root / "legacy_fixture"
        fixture.mkdir(exist_ok=True)
        route = fixture / f"tab_{slug}.py"
        route.write_text(f'''from fastapi import APIRouter
from services.{slug}_service import answer
TAB_MANIFEST = {{"id": "{slug}", "label": "{slug.title()}"}}
router = APIRouter(prefix="/legacy/{slug}")
@router.get("")
def value(): return {{"answer": answer}}
''', encoding="utf-8")
        service = fixture / f"{slug}_service.py"
        service.write_text("answer = 99\n", encoding="utf-8")
        view = fixture / f"{slug}.js"
        view.write_text("export default {};\n", encoding="utf-8")
        for source, target in ((route, self.routes), (service, self.services), (view, self.views)):
            shutil.copyfile(source, target / source.name)

    def test_legacy_absolute_imports_urls_and_whole_tree_approval_survive(self):
        self.legacy()
        self.legacy("minecraft")
        self.approve("jobs")
        self.assertFalse(tabs.user_tab_is_approved("minecraft"))
        self.approve("minecraft")
        tabs.mount_all(self.app)
        self.assertEqual(self.client.get("/legacy/jobs").json(), {"answer": 99})
        self.assertEqual(self.client.get("/custom-views/jobs.js").content, (self.views / "jobs.js").read_bytes())
        manifest = next(m for m in tabs.list_manifests() if m["id"] == "jobs")
        self.assertEqual(manifest["format"], "legacy")
        self.assertIsNone(manifest["style_url"])
        self.folder("alpha")
        self.approve("alpha")
        self.assertTrue(tabs.user_tab_is_approved("jobs"))
        (self.services / "minecraft_service.py").write_text("answer = 0\n", encoding="utf-8")
        self.assertFalse(tabs.user_tab_is_approved("jobs"))
        self.assertFalse(tabs.user_tab_is_approved("minecraft"))
        self.assertTrue(tabs.user_tab_is_approved("alpha"))

    def test_user_override_wins_even_before_approval(self):
        self.folder(user=False, answer=1)
        self.folder(answer=2)
        tabs.set_template_enabled("alpha", True)
        self.assertFalse(tabs.mount_one(self.app, "alpha"))
        self.assertIsNone(tabs.folder_file_bytes("alpha", "view.js"))
        self.approve("alpha")
        self.assertTrue(tabs.mount_one(self.app, "alpha"))
        self.assertEqual(self.client.get("/fixture/alpha").json()["answer"], 2)
        self.assertTrue(next(m for m in tabs.list_manifests() if m["id"] == "alpha")["user_tab"])
        self.assertEqual(self.status("alpha", "prebuilt")["status"], "off")

    def test_school_loads_only_when_enabled_and_crm_stays_in_gallery(self):
        with patch.object(tabs, "PREBUILT_TABS_DIR", str(REPO / "tabs")):
            self.assertEqual({e["slug"] for e in tabs.list_templates()}, {"school", "crm"})
            school = next(e for e in tabs.list_templates() if e["slug"] == "school")
            self.assertIn("courses", school["blurb"])
            self.assertEqual(self.status("school", "prebuilt")["status"], "off")
            self.assertFalse(tabs.mount_one(self.app, "school"))
            self.assertNotIn("kairos_tabs.school", sys.modules)
            self.assertIsNone(tabs.folder_file_bytes("school", "view.js"))
            tabs.set_template_enabled("school", True)
            self.assertTrue(tabs.mount_one(self.app, "school"))
            mod = sys.modules["kairos_tabs.school.routes"]
            self.assertEqual(Path(mod.__file__), REPO / "tabs" / "school" / "routes.py")
            self.assertEqual(self.client.get("/api/tab-school/courses").json(), [])
            self.assertEqual(self.client.get("/tab-files/school/view.js").status_code, 200)
            self.assertEqual(self.client.get("/api/tab-school/assignments/missing/draft").status_code, 200)
            tabs.set_template_enabled("school", False)
            tabs.discover()
            self.assertNotIn("kairos_tabs.school", sys.modules)
            self.assertEqual(self.client.get("/tab-files/school/view.js").status_code, 404)
            self.assertNotIn("school", {m["id"] for m in tabs.list_manifests()})

    def test_files_refuse_traversal_and_python_source(self):
        self.folder()
        self.approve("alpha")
        for slug, filename in (("../alpha", "view.js"), ("alpha/..", "view.js"), ("alpha", "../view.js"),
                                ("alpha", "routes.py"), ("alpha", "tab.json"), ("alpha", "view.js/..")):
            self.assertIsNone(tabs.folder_file_bytes(slug, filename))
        for url in ("/tab-files/alpha/routes.py", "/tab-files/alpha/tab.json", "/tab-files/alpha/%2e%2e%2fview.js"):
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_serving_rechecks_bytes_after_read(self):
        folder = self.folder()
        self.approve("alpha")
        original = tab_folders.fingerprint
        calls = 0
        def change_during_read(*args, **kwargs):
            nonlocal calls
            calls += 1
            # file_bytes is tested directly to place the edit precisely
            # between its before/after captures, not in metadata scanning.
            if calls == 2:
                (folder / "view.js").write_text("unapproved bytes", encoding="utf-8")
            return original(*args, **kwargs)
        entry = tabs._candidates()["alpha"]
        with patch.object(tab_folders, "fingerprint", side_effect=change_during_read):
            self.assertIsNone(tab_folders.file_bytes(entry, "view.js", lambda _e: True))
        self.assertIsNone(tabs.folder_file_bytes("alpha", "view.js"))

    def test_symlinked_file_is_invalid_and_never_served(self):
        folder = self.folder()
        self.approve("alpha")
        outside = self.root / "outside.js"
        outside.write_text("outside", encoding="utf-8")
        view = folder / "view.js"
        view.unlink()
        try:
            view.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertEqual(self.status("alpha")["status"], "invalid")
        self.assertIsNone(tabs.folder_file_bytes("alpha", "view.js"))

    def test_link_and_junction_checks_include_bytecode_caches(self):
        folder = self.folder()
        cache = folder / "__pycache__"
        cache.mkdir()
        (cache / "planted.pyc").write_bytes(b"planted")
        with patch.object(os.path, "isjunction", side_effect=lambda p: Path(p) == cache, create=True):
            self.assertEqual(self.status("alpha")["status"], "invalid")
        self.approve("alpha")
        self.assertFalse(cache.exists())

    def test_prebuilt_loader_never_needs_install_folder_writes(self):
        folder = self.folder(user=False)
        cache = folder / "__pycache__"
        cache.mkdir()
        (cache / "service.cpython-313.pyc").write_bytes(b"planted")
        tabs.set_template_enabled("alpha", True)
        with patch.object(shutil, "rmtree", side_effect=PermissionError("read-only install")):
            tabs.mount_all(self.app)
        self.assertEqual(self.client.get("/fixture/alpha").json()["answer"], 42)
        self.assertEqual(list(cache.iterdir()), [cache / "service.cpython-313.pyc"])

    def test_lazy_import_uses_source_and_is_blocked_after_edit(self):
        folder = self.folder()
        (folder / "late.py").write_text("answer = 5\n", encoding="utf-8")
        self.approve("alpha")
        tabs.discover()
        self.assertEqual(importlib.import_module("kairos_tabs.alpha.late").answer, 5)
        self.assertFalse((folder / "__pycache__").exists())
        (folder / "later.py").write_text("answer = 6\n", encoding="utf-8")
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("kairos_tabs.alpha.later")

    def test_status_endpoint_hides_review_material_from_non_admins(self):
        self.folder()
        for admin in (False, True):
            with self.subTest(admin=admin), patch.object(system_routes.auth_manager, "is_admin", return_value=admin):
                response = self.client.get("/api/system/tabs")
                self.assertEqual(response.status_code, 200)
                entry = next(e for e in response.json() if e["slug"] == "alpha")
                self.assertEqual("fingerprint" in entry, admin)
                self.assertEqual("files" in entry, admin)
                if admin:
                    approved = self.client.post("/api/system/custom-tabs/alpha/approve", json={"fingerprint": entry["fingerprint"]})
                    self.assertEqual(approved.status_code, 200)

    def test_failed_router_does_not_take_down_other_tabs(self):
        folder = self.folder()
        (folder / "routes.py").write_text("router = None\n", encoding="utf-8")
        self.approve("alpha")
        self.folder("beta")
        self.approve("beta")
        tabs.mount_all(self.app)
        self.assertEqual(self.status("alpha")["status"], "failed")
        self.assertIn("APIRouter", self.status("alpha")["reason"])
        self.assertEqual(self.client.get("/fixture/beta").status_code, 200)

    def test_deleting_override_preserves_shipped_folder(self):
        self.folder(user=False)
        self.folder()
        self.approve("alpha")
        tabs.delete("alpha")
        self.assertTrue((self.prebuilt / "alpha" / "routes.py").is_file())
        self.assertFalse((self.users / "alpha").exists())
        with self.assertRaises(ValueError):
            tabs.delete("../tabs")

    def test_legacy_user_overrides_folder_prebuilt_without_rewriting_source(self):
        self.folder("jobs", user=False, answer=1)
        self.legacy()
        tabs.set_template_enabled("jobs", True)
        original = (self.routes / "tab_jobs.py").read_bytes()
        self.approve("jobs")
        tabs.mount_all(self.app)
        self.assertEqual(self.client.get("/legacy/jobs").json()["answer"], 99)
        self.assertEqual(self.client.get("/fixture/jobs").status_code, 404)
        self.assertNotIn("kairos_tabs.jobs", sys.modules)
        self.assertIsNone(tabs.folder_file_bytes("jobs", "view.js"))
        self.assertEqual((self.routes / "tab_jobs.py").read_bytes(), original)

    def test_relative_subpackages_work_and_router_mount_is_idempotent(self):
        folder = self.folder()
        helpers = folder / "helpers"
        helpers.mkdir()
        (helpers / "__init__.py").write_text("from .values import answer\n", encoding="utf-8")
        (helpers / "values.py").write_text("answer = 17\n", encoding="utf-8")
        (folder / "service.py").write_text("from .helpers import answer\n", encoding="utf-8")
        self.approve("alpha")
        tabs.mount_all(self.app)
        tabs.mount_one(self.app, "alpha")
        self.assertEqual(self.client.get("/fixture/alpha").json()["answer"], 17)
        self.assertIs(self.app.state.custom_tab_modules["alpha"], sys.modules["kairos_tabs.alpha.routes"])
        self.assertEqual(len(self.app.state.custom_tab_routes["alpha"]), 1)
        self.assertFalse(list(folder.rglob("__pycache__")))

    def test_fingerprints_include_assets_paths_and_removals(self):
        folder = self.folder()
        assets = folder / "assets"
        assets.mkdir()
        asset = assets / "one.txt"
        asset.write_text("same bytes", encoding="utf-8")
        first = self.approve("alpha")
        self.assertIn("assets/one.txt", first["files"])
        asset.rename(assets / "two.txt")
        self.assertFalse(tabs.user_tab_is_approved("alpha"))
        self.approve("alpha")
        (assets / "two.txt").unlink()
        self.assertFalse(tabs.user_tab_is_approved("alpha"))

    def hook_folder(self, slug="alpha", *, user=True, hooks=None, source=None):
        folder = self.folder(slug, user=user, hooks=hooks or ["start", "stop", "on_message", "calendar_items"])
        (folder / "hooks.py").write_text(source or """from core import tab_api
api = tab_api.for_tab(__package__)
events = []
async def start(): events.append("start")
async def stop(): events.append("stop")
def on_message(source, message): events.append((source, message))
def calendar_items(user, start, end):
    return [{"id": "fixture", "title": user, "start": start, "end": end,
             "source": "tab", "source_label": "Fixture", "toggle_url": "/api/fixture/completed"}]
""", encoding="utf-8")
        if user:
            self.approve(slug)
        return folder

    def test_single_tab_policy_import_and_serve_never_scan_neighbours(self):
        folder = self.folder()
        (folder / "late.py").write_text("answer = 7\n", encoding="utf-8")
        self.folder("beta")
        self.approve("alpha")
        tabs.discover()
        original = tab_folders.fingerprint
        calls = []
        def fingerprint(folder, **kwargs):
            calls.append(Path(folder).name)
            return original(folder, **kwargs)
        with patch.object(tab_folders, "fingerprint", side_effect=fingerprint), \
             patch.object(tabs, "_folder_entries", side_effect=AssertionError("full scan")):
            self.assertEqual(importlib.import_module("kairos_tabs.alpha.late").answer, 7)
            self.assertTrue(tabs.user_tab_is_approved("alpha"))
            self.assertIsNotNone(tabs.folder_file_bytes("alpha", "view.js"))
            (self.users / "alpha" / "__pycache__").mkdir(exist_ok=True)
            self.assertTrue(tabs._folder_allowed(tab_folders._registered["alpha"]))
        self.assertEqual(set(calls), {"alpha"})

    def test_tab_api_confinement_and_one_time_adoption(self):
        self.folder()
        self.approve("alpha")
        tabs.discover()
        with patch.object(tab_api, "DATA_DIR", str(self.root / "data")):
            api = tab_api.for_tab("kairos_tabs.alpha")
            directory = self.root / "data/tab-data/alpha"
            self.assertFalse(directory.exists())
            self.assertEqual(Path(api.data_dir), directory)
            for name in ("../escape.json", "sub/file.json", "C:\\escape.json", "file:stream", "..", "CON.json"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    api.write_json(name, {})
            self.assertEqual(api.read_json("missing.json", []), [])
            old = self.root / "data/alpha.json"
            old.write_text('{"kept": 1}', encoding="utf-8")
            api.adopt_data_file("alpha.json")
            self.assertFalse(old.exists())
            self.assertEqual(api.read_json("alpha.json", {}), {"kept": 1})
            old.write_text('{"kept": 2}', encoding="utf-8")
            api.adopt_data_file("alpha.json")
            self.assertTrue(old.exists())
            api.write_json("alpha.json", {"kept": 3})
            self.assertEqual(api.read_json("alpha.json", {}), {"kept": 3})
            with self.assertRaises(ValueError):
                api.adopt_data_file("settings.json")
            with self.assertRaises(ValueError):
                tab_api.for_tab("core")
            with self.assertRaises(ValueError):
                tab_api.for_tab("kairos_tabs.unregistered")

    def test_encryption_is_owned_by_one_tab_and_migration_is_idempotent(self):
        from core import secret_storage
        api = tab_api.TabAPI("alpha", {})
        other = tab_api.TabAPI("beta", {})
        own = api.encrypt("canvas-token")
        self.assertEqual(api.decrypt(own), "canvas-token")
        for token in (secret_storage.encrypt("email-password"), other.encrypt("model-key")):
            with self.assertRaises(ValueError):
                api.decrypt(token)
        with patch.object(tab_api, "DATA_DIR", str(self.root / "data")):
            old = self.root / "data/alpha.json"
            legacy = secret_storage.encrypt("old-canvas-token")
            old.write_text(json.dumps({"nested": [{"token": legacy}], "plain": "kept"}), encoding="utf-8")
            api.adopt_data_file("alpha.json")
            adopted = api.read_json("alpha.json", {})
            token = adopted["nested"][0]["token"]
            self.assertEqual(api.decrypt(token), "old-canvas-token")
            self.assertNotEqual(token, legacy)
            api.adopt_data_file("alpha.json")
            self.assertEqual(api.read_json("alpha.json", {}), adopted)
            # Simulate data already moved by A2, and a plain token discovered
            # later. Only core adoption can convert it, once per value.
            api.write_json("alpha.json", {"token": legacy, "own": own})
            api.adopt_data_file("alpha.json")
            converted = api.read_json("alpha.json", {})
            self.assertEqual(api.decrypt(converted["token"]), "old-canvas-token")
            self.assertEqual(converted["own"], own)
            api.adopt_data_file("alpha.json")
            self.assertEqual(api.read_json("alpha.json", {}), converted)

    def archive(self, slug="portable", extras=None, **meta):
        manifest = dict(slug=slug, name="Portable", version="1", description="A portable tab", api=1, hooks=[])
        manifest.update(meta)
        files = {f"{slug}/tab.json": json.dumps(manifest),
                 f"{slug}/routes.py": "from fastapi import APIRouter\nrouter = APIRouter()\n",
                 f"{slug}/view.js": "export function render() {}\n"}
        files.update(extras or {})
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items(): archive.writestr(name, content)
        return output.getvalue()

    def install_archive(self, content=None, **options):
        from core import tab_install
        with patch.object(tab_install, "DATA_DIR", str(self.root / "data")):
            return self.client.post("/api/system/tabs/install", files={"file": ("portable.kairostab", content or self.archive())}, data=options)

    def test_export_install_roundtrip_stays_unapproved_and_replace_revokes(self):
        folder = self.folder("portable")
        self.approve("portable")
        exported = self.client.get("/api/system/tabs/portable/export")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("portable.kairostab", exported.headers["content-disposition"])
        expected = {p.name: p.read_bytes() for p in folder.iterdir()}
        self.assertEqual(self.install_archive(exported.content).status_code, 400)
        (folder / "obsolete.txt").write_text("remove me", encoding="utf-8")
        self.install_archive(exported.content, replace="true").raise_for_status()
        self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, expected)
        entry = next(e for e in tabs.list_tabs() if e["slug"] == "portable")
        self.assertEqual(entry["status"], "needs_approval")
        self.assertNotIn("portable", self.saved[tabs._FOLDER_APPROVALS_KEY])
        self.assertFalse(tabs.mount_one(self.app, "portable"))
        self.assertEqual(self.client.get("/tab-files/portable/view.js").status_code, 404)

    def test_install_refuses_unsafe_archive_paths_and_special_files(self):
        for name in ("/escape", "../escape", "portable/../escape", "C:/escape", "portable/C:escape", "portable\\escape",
                     "other/file.txt", "portable/CON.txt", "portable/alias.", "portable//file.txt", "portable/__pycache__/x.pyc"):
            with self.subTest(name=name):
                # ZipInfo normalizes the writer's Windows separators. Put a
                # literal backslash into both zip filename records instead.
                content = self.archive(extras={name.replace("\\", "/"): "bad"})
                if "\\" in name:
                    content = content.replace(name.replace("\\", "/").encode(), name.encode())
                self.assertEqual(self.install_archive(content).status_code, 400)
                self.assertFalse((self.users / "portable").exists())
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
            buffer = io.BytesIO(self.archive())
            with zipfile.ZipFile(buffer, "a") as archive:
                info = zipfile.ZipInfo("portable/special"); info.create_system = 3; info.external_attr = (mode | 0o777) << 16
                archive.writestr(info, "outside")
            self.assertEqual(self.install_archive(buffer.getvalue()).status_code, 400)

    def test_install_limits_metadata_python_and_binary_checks(self):
        from core import tab_install
        for options in ({"api": "1"}, {"reads": []}, {"slug": "wrong"}):
            content = self.archive(**options) if "slug" not in options else self.archive(extras={"portable/tab.json": json.dumps(dict(slug="wrong"))})
            self.assertEqual(self.install_archive(content).status_code, 400)
        for filename, content in (("routes.py", "not python !!!"), ("helper.py", "from core import settings"),
                                  ("helper.py", "import services.email_service"), ("helper.py", "from core.tab_api import complete\nfrom app import app"),
                                  ("bad.pyc", b"compiled"), ("bad.dll", b"MZ"), ("bad.txt", b"\x00binary"), ("bad.unknown", b"\xff\xfe")):
            with self.subTest(file=filename, content=content):
                self.assertEqual(self.install_archive(self.archive(extras={"portable/" + filename: content})).status_code, 400)
        for limit, value in (("MAX_COMPRESSED", 10), ("MAX_UNPACKED", 10), ("MAX_FILES", 2)):
            with patch.object(tab_install, limit, value):
                self.assertEqual(self.install_archive().status_code, 400)
        self.folder("portable", user=False)
        self.assertEqual(self.install_archive(replace="true").status_code, 400)
        self.assertEqual(self.client.get("/api/system/tabs/portable/export").status_code, 400)
        self.assertEqual(self.client.get("/api/system/tabs/missing/export").status_code, 400)

    def test_install_scans_every_file_and_caution_requires_exact_confirmation(self):
        from core import tab_install
        from services import skills_guard
        finding = skills_guard.Finding("fixture", "high", "execution", "extra.unknown", 1, "fixture", "Review this")
        # Unknown extensions and ignore files still get the shared scanner.
        content = self.archive(extras={"portable/extra.unknown": "flag", "portable/.skillignore": "extra.unknown"})
        with patch.object(skills_guard, "scan_file", side_effect=lambda path, *a, **kw: [finding] if path.name == "extra.unknown" else []) as scanner:
            response = self.install_archive(content)
            self.assertEqual(response.status_code, 409)
            detail = response.json()["detail"]
            self.assertTrue(detail["needs_confirmation"])
            self.assertFalse((self.users / "portable").exists())
            self.assertEqual(self.install_archive(content, confirmed="true").status_code, 409)
            self.assertEqual(self.install_archive(content, confirmed="true", expected_fingerprint="wrong").status_code, 409)
            self.install_archive(content, confirmed="true", expected_fingerprint=detail["fingerprint"]).raise_for_status()
            self.assertTrue(all(call.kwargs["force_text"] for call in scanner.call_args_list))
        finding.severity = "critical"
        with patch.object(skills_guard, "scan_file", return_value=[finding]):
            response = self.install_archive(content, replace="true", confirmed="true", expected_fingerprint=detail["fingerprint"])
            self.assertEqual(response.status_code, 409)
            self.assertFalse(response.json()["detail"]["needs_confirmation"])

    def test_github_install_uses_contents_api_and_refuses_links_and_other_hosts(self):
        from core import tab_install
        meta = dict(slug="portable", name="Portable", version="1", description="", api=1, hooks=[])
        sources = {"tab.json": json.dumps(meta).encode(), "routes.py": b"from fastapi import APIRouter\nrouter = APIRouter()\n", "nested/helper.py": b"from core import tab_api\n"}
        folder = "plugins/portable"
        def network(url):
            from urllib.parse import urlsplit, unquote, parse_qs
            parsed = urlsplit(url)
            self.assertEqual(parsed.netloc, "api.github.com")
            self.assertEqual(parse_qs(parsed.query), {"ref": ["main"]})
            path = unquote(parsed.path).split("/contents/", 1)[1]
            if path == folder:
                return [{"name": n, "path": path + "/" + n, "type": "dir" if n == "nested" else "file", "size": 1} for n in ("tab.json", "routes.py", "nested")]
            if path == folder + "/nested":
                return [{"name": "helper.py", "path": path + "/helper.py", "type": "file", "size": 1}]
            data = sources[path[len(folder) + 1:]]
            return {"type": "file", "encoding": "base64", "size": len(data), "content": base64.b64encode(data).decode()}
        with patch.object(tab_install, "_github_json", side_effect=network), patch.object(tab_install, "DATA_DIR", str(self.root / "data")):
            response = self.client.post("/api/system/tabs/install", json={"github_url": "https://github.com/owner/repo/tree/main/plugins/portable"})
            response.raise_for_status()
            self.assertEqual(response.json()["status"], "needs_approval")
        for url in ("https://evil.com/a/b/tree/main/portable", "http://github.com/a/b/tree/main/portable", "https://github.com/a/b/blob/main/portable", "https://github.com/a/b/tree/main/../portable", "https://github.com:443/a/b/tree/main/portable"):
            with patch.object(tab_install, "_github_json") as forbidden_network:
                with self.assertRaises(ValueError): tab_install.github_archive(url)
                forbidden_network.assert_not_called()
        for kind in ("symlink", "submodule"):
            with patch.object(tab_install, "_github_json", return_value=[{"name": "escape", "path": folder + "/escape", "type": kind}]):
                with self.assertRaises(ValueError): tab_install.github_archive("https://github.com/owner/repo/tree/main/plugins/portable")
        # Branches containing slashes still use the same bounded API path.
        import httpx
        request = httpx.Request("GET", "https://api.github.com/contents")
        missing = httpx.HTTPStatusError("Not found", request=request, response=httpx.Response(404, request=request))
        calls = []
        def slash_ref(url):
            calls.append(url)
            if "ref=feature%2Ftabs" not in url:
                raise missing
            return network(url.replace("ref=feature%2Ftabs", "ref=main"))
        with patch.object(tab_install, "_github_json", side_effect=slash_ref):
            content = tab_install.github_archive("https://github.com/owner/repo/tree/feature/tabs/plugins/portable")
            self.assertTrue(content)
            self.assertIn("ref=feature%2Ftabs", calls[1])

    def test_non_admin_cannot_install_export_approve_or_enable(self):
        from fastapi import HTTPException
        def denied(): raise HTTPException(status_code=403, detail="Admin required")
        self.app.dependency_overrides[require_admin] = denied
        for method, url, options in (
            ("post", "/api/system/tabs/install", {"json": {"github_url": "https://github.com/o/r/tree/main/tab"}}),
            ("get", "/api/system/tabs/alpha/export", {}),
            ("post", "/api/system/custom-tabs/alpha/approve", {"json": {"fingerprint": "hash"}}),
            ("post", "/api/system/tab-templates/alpha", {"json": {"enabled": True}})):
            self.assertEqual(getattr(self.client, method)(url, **options).status_code, 403)

    def test_tab_api_rejects_linked_data(self):
        self.folder()
        self.approve("alpha")
        tabs.discover()
        with patch.object(tab_api, "DATA_DIR", str(self.root / "data")):
            api = tab_api.for_tab("kairos_tabs.alpha")
            api.write_json("test.json", {})
            target = Path(api.data_dir) / "test.json"
            with patch.object(tab_folders, "is_link", side_effect=lambda p: Path(p) == target):
                with self.assertRaises(ValueError):
                    api.read_json("test.json", {})
            parent = self.root / "data/tab-data"
            with patch.object(tab_folders, "is_link", side_effect=lambda p: Path(p) == parent):
                with self.assertRaises(ValueError):
                    api.write_json("test.json", {})

    def test_mailbox_exposes_only_read_only_operations(self):
        from unittest.mock import MagicMock
        from services.email_service import email_service
        box = MagicMock()
        box.__enter__.return_value = box
        box.select.return_value = ("OK", [])
        account = {"imap_host": "mail", "imap_port": 993, "email": "person@test", "password_encrypted": "opaque"}
        with patch.object(email_service, "get_account", return_value=account), \
             patch.object(tab_api.imaplib, "IMAP4_SSL", return_value=box), \
             patch.object(tab_api.secret_storage, "decrypt", return_value="password"):
            with tab_api.open_mailbox("id", "INBOX") as mailbox:
                box.select.assert_called_once_with('"INBOX"', readonly=True)
                mailbox.uid("search", None, "ALL")
                mailbox.uid("fetch", b"1", "(RFC822.SIZE BODY.PEEK[])")
                mailbox.response("UIDVALIDITY")
                for command, args in (("store", (b"1", "+FLAGS", "\\Seen")),
                        ("fetch", (b"1", "(BODY[])")), ("fetch", (b"1", "(RFC822)")),
                        ("fetch", (b"1", "(BODY.PEEK[] FLAGS)"))):
                    with self.assertRaises(ValueError):
                        mailbox.uid(command, *args)
                with self.assertRaises(ValueError):
                    mailbox.uid("search", None, "ALL\r\nCREATE mailbox")
                with self.assertRaises(ValueError):
                    mailbox.response("PASSWORD")
                self.assertFalse(hasattr(mailbox, "login"))
                self.assertFalse(hasattr(mailbox, "select"))
                self.assertFalse(hasattr(mailbox, "password"))
            box.__exit__.assert_called_once()

    def test_complete_refuses_codex_and_records_tool_free_usage(self):
        from core import model_endpoints
        with patch.object(model_endpoints, "get_endpoint", return_value={"kind": "codex_cli"}), \
             patch("core.providers.openai_compatible.run_turn", new_callable=AsyncMock) as provider:
            with self.assertRaisesRegex(ValueError, "tool-free"):
                asyncio.run(tab_api.complete("codex", "system", "prompt"))
            provider.assert_not_called()
        async def reply(*args, **kwargs):
            kwargs["on_usage"]({"input_tokens": 2, "output_tokens": 1})
            return "answer"
        with patch.object(model_endpoints, "get_endpoint", return_value={"kind": "local"}), \
             patch.object(model_endpoints, "resolve_runtime", return_value=("http://test", "model", "key", 8192)), \
             patch("core.providers.openai_compatible.run_turn", new=AsyncMock(side_effect=reply)) as provider, \
             patch.object(tab_api.token_usage, "record_usage") as usage:
            self.assertEqual(asyncio.run(tab_api.complete("local", "system", "prompt")), "answer")
            self.assertIsNone(provider.call_args.kwargs["tools"])
            self.assertIsNone(provider.call_args.kwargs["tool_executor"])
            usage.assert_called_once_with("local", {"input_tokens": 2, "output_tokens": 1})

    def test_adoption_cannot_take_an_app_credentials_store(self):
        self.folder("model_endpoints")
        self.approve("model_endpoints")
        tabs.discover()
        api = tab_api.for_tab("kairos_tabs.model_endpoints")
        with self.assertRaises(ValueError):
            api.adopt_data_file("model_endpoints.json")

    def test_complete_enforces_its_timeout(self):
        async def wait(*args, **kwargs):
            await asyncio.sleep(10)
        with patch.object(tab_api.model_endpoints, "get_endpoint", return_value={"kind": "local"}), \
             patch.object(tab_api.model_endpoints, "resolve_runtime", return_value=("http://test", "model", None, 8192)), \
             patch("core.providers.openai_compatible.run_turn", new=wait):
            with self.assertRaises(TimeoutError):
                asyncio.run(tab_api.complete("local", "system", "prompt", timeout=.02))

    def test_model_and_email_metadata_never_include_credentials(self):
        with patch.object(tab_api.model_endpoints, "list_endpoints", return_value=[{
                "id": "m", "name": "Model", "kind": "api", "model": "test", "api_key": "secret"}]):
            self.assertEqual(tab_api.list_models(), [{"id": "m", "name": "Model", "kind": "api", "model": "test"}])
        with patch("services.email_service.email_service.list_accounts", return_value=[{
                "id": "e", "email": "a@test", "name": "Work", "password_encrypted": "secret"}]):
            self.assertEqual(tab_api.email_accounts(), [{"id": "e", "email": "a@test", "name": "Work"}])

    def test_hooks_only_run_for_on_tabs_that_declare_them(self):
        self.hook_folder()
        self.hook_folder("beta", user=False)
        self.hook_folder("gamma", hooks=["calendar_items"])
        tabs.discover()
        async def run():
            await tab_hooks.reconcile()
            alpha = sys.modules["kairos_tabs.alpha.hooks"]
            gamma = sys.modules["kairos_tabs.gamma.hooks"]
            self.assertEqual(alpha.events, ["start"])
            self.assertEqual(gamma.events, [])
            self.assertNotIn("kairos_tabs.beta", sys.modules)
            result = await tab_hooks.calendar_items("owner", "first", "last")
            self.assertEqual(len(result), 2)
            tab_hooks.emit_message({"kind": "connector", "connection_id": "id"}, {"message_id": "1"})
            await asyncio.sleep(.1)
            self.assertEqual(len(alpha.events), 2)
            self.assertEqual(gamma.events, [])
            # Edits revoke hook access without requiring another discovery pass.
            (self.users / "alpha/view.js").write_text("changed", encoding="utf-8")
            self.assertEqual(len(await tab_hooks.calendar_items("owner", "first", "last")), 1)
            await tab_hooks.reconcile()
            self.assertEqual(alpha.events[-1], "stop")
            await tab_hooks.stop_all()
        asyncio.run(run())

    def test_hook_exception_and_timeout_isolation_without_reply_delay(self):
        source = """import asyncio, time
async def on_message(source, message):
    await asyncio.sleep(10)
def calendar_items(user, start, end):
    time.sleep(.6)
    return []
"""
        self.hook_folder(source=source, hooks=["on_message", "calendar_items"])
        self.hook_folder("beta", source="def calendar_items(*args): raise ValueError('broken')", hooks=["calendar_items"])
        self.hook_folder("gamma")
        tabs.discover()
        async def run():
            with patch.object(tab_hooks, "TIMEOUT", .2), self.assertLogs("core.tab_hooks", level="WARNING") as logs:
                before = time.monotonic()
                tab_hooks.emit_message({"kind": "connector", "connection_id": "id"}, {"text": "hi"})
                self.assertLess(time.monotonic() - before, .02)
                result = await tab_hooks.calendar_items("owner", "first", "last")
                self.assertEqual(len(result), 1)
                self.assertLess(time.monotonic() - before, .45)
                await asyncio.sleep(.25)
            self.assertTrue(any("exceeded" in line for line in logs.output))
            self.assertTrue(any("failed" in line for line in logs.output))
        asyncio.run(run())

    def test_missing_and_exit_hooks_cannot_break_lifecycle_or_calendar(self):
        self.hook_folder(source="def calendar_items(*args): raise SystemExit('tab exit')")
        self.hook_folder("beta")
        tabs.discover()
        async def run():
            with self.assertLogs("core.tab_hooks", level="ERROR"):
                await tab_hooks.reconcile()
                items = await tab_hooks.calendar_items("user", "first", "last")
                self.assertEqual(len(items), 1)
                await tab_hooks.stop_all()
        asyncio.run(run())

    def test_lifecycle_enable_disable_reenable_approval_and_delete(self):
        self.hook_folder(user=False)
        self.folder("beta")
        self.client.post("/api/system/tab-templates/alpha", json={"enabled": True}).raise_for_status()
        old = sys.modules["kairos_tabs.alpha.hooks"]
        self.assertEqual(old.events, ["start"])
        self.client.post("/api/system/tab-templates/alpha", json={"enabled": False}).raise_for_status()
        self.assertEqual(old.events, ["start", "stop"])
        self.assertEqual(self.client.get("/fixture/alpha").status_code, 409)
        self.client.post("/api/system/tab-templates/alpha", json={"enabled": True}).raise_for_status()
        current = sys.modules["kairos_tabs.alpha.hooks"]
        self.assertIsNot(old, current)
        self.assertEqual(current.events, ["start"])
        self.assertEqual(self.client.get("/fixture/alpha").status_code, 200)
        folder = self.users / "beta"
        (folder / "hooks.py").write_text((self.prebuilt / "alpha/hooks.py").read_text(), encoding="utf-8")
        meta = json.loads((folder / "tab.json").read_text())
        meta["hooks"] = ["start", "stop"]
        (folder / "tab.json").write_text(json.dumps(meta), encoding="utf-8")
        pending = next(e for e in tabs.pending_approvals() if e["id"] == "beta")
        self.client.post("/api/system/custom-tabs/beta/approve", json={"fingerprint": pending["fingerprint"]}).raise_for_status()
        beta = sys.modules["kairos_tabs.beta.hooks"]
        self.assertEqual(beta.events, ["start"])
        self.assertEqual(self.client.get("/fixture/beta").status_code, 200)
        self.client.delete("/api/system/custom-tabs/beta").raise_for_status()
        self.assertEqual(beta.events, ["start", "stop"])
        self.assertEqual(self.client.get("/fixture/beta").status_code, 409)

    def test_calendar_merges_tab_items_and_owned_toggle(self):
        from routes import calendar_routes
        with patch.object(tabs, "PREBUILT_TABS_DIR", str(REPO / "tabs")):
            tabs.set_template_enabled("crm", True)
            tabs.mount_one(self.app, "crm")
            self.app.include_router(calendar_routes.router)
            route = sys.modules["kairos_tabs.crm.routes"]
            task = route.crm_service.create_task("reviewer", {"title": "Calendar follow-up", "due_date": "2026-10-09"})
            with patch.object(calendar_routes.calendar_service, "list_range", return_value=[{"id": "base"}]):
                response = self.client.get("/api/calendar/events", params={"start": "2026-10-09T00:00:00Z", "end": "2026-10-10T00:00:00Z"})
            response.raise_for_status()
            items = response.json()
            self.assertEqual(items[0]["id"], "base")
            item = next(i for i in items if i.get("id") == task["id"])
            self.assertEqual(item["source"], "tab")
            self.assertEqual(item["source_label"], "From CRM")
            self.client.patch(item["toggle_url"], json={"completed": True}).raise_for_status()
            self.assertEqual(route.crm_service.task("reviewer", task["id"])["status"], "done")
            self.client.patch(item["toggle_url"], json={"completed": False}).raise_for_status()
            self.assertEqual(route.crm_service.task("reviewer", task["id"])["status"], "active")
            self.app.dependency_overrides[require_user] = lambda: "other"
            self.assertEqual(self.client.patch(item["toggle_url"], json={"completed": True}).status_code, 404)

    def test_school_sync_starts_and_stops_and_preserves_chats_and_tokens(self):
        from core import sync_engine
        with patch.object(tabs, "PREBUILT_TABS_DIR", str(REPO / "tabs")):
            self.client.post("/api/system/tab-templates/school", json={"enabled": True}).raise_for_status()
            self.assertIn("School (Canvas)", sync_engine.list_providers())
            route = sys.modules["kairos_tabs.school.routes"]
            settings = self.client.put("/api/tab-school/settings", json={"canvas_api_token": "test-token"})
            self.assertTrue(settings.json()["canvas_api_token_configured"])
            stored = route.school_service._data["settings"]["canvas_api_token_encrypted"]
            self.assertNotEqual(stored, "test-token")
            self.assertEqual(route.api.decrypt(stored), "test-token")
            first = self.client.get("/api/tab-school/courses/session", params={"course": "CS/405"}).json()
            self.assertEqual(first, self.client.get("/api/tab-school/courses/session", params={"course": "CS/405"}).json())
            self.client.post("/api/system/tab-templates/school", json={"enabled": False}).raise_for_status()
            self.assertNotIn("School (Canvas)", sync_engine.list_providers())
            self.assertEqual(self.client.get("/api/tab-school/courses").status_code, 409)

    def test_school_adopts_app_key_token_and_syncs_with_scoped_token(self):
        from core import secret_storage
        data = self.root / "data"
        old = data / "school.json"
        old.write_text(json.dumps({"settings": {"canvas_base_url": "https://canvas.example",
            "canvas_api_token_encrypted": secret_storage.encrypt("legacy-canvas")}}), encoding="utf-8")
        with patch.object(tabs, "PREBUILT_TABS_DIR", str(REPO / "tabs")), patch.object(tab_api, "DATA_DIR", str(data)):
            self.client.post("/api/system/tab-templates/school", json={"enabled": True}).raise_for_status()
            service = sys.modules["kairos_tabs.school.service"]
            stored = service.school_service._data["settings"]["canvas_api_token_encrypted"]
            self.assertFalse(old.exists())
            self.assertEqual(service.api.decrypt(stored), "legacy-canvas")
            with patch.object(service, "_sync_from_api", new_callable=AsyncMock, return_value=[]) as sync:
                asyncio.run(service.school_service.sync())
                sync.assert_awaited_once_with("https://canvas.example", "legacy-canvas")
            service.api.adopt_data_file("school.json")
            self.assertEqual(service.api.read_json("school.json", {})["settings"]["canvas_api_token_encrypted"], stored)

    def test_folder_tab_source_write_roots_and_file_tools_stay_in_data(self):
        from core import codex_brain, brain, memory_tools
        self.folder()
        config = self.root / "codex"
        config.mkdir()
        (config / "config.toml").write_text('sandbox_workspace_write.writable_roots = ["retained-root"]\n', encoding="utf-8")
        with patch.object(codex_brain, "USER_TABS_DIR", str(self.users)), patch.dict(os.environ, {"CODEX_HOME": str(config)}):
            self.assertEqual(json.loads(codex_brain._writable_roots_override()), ["retained-root", str(self.users)])
        self.assertTrue(brain._is_user_tab_source_path(str(self.users / "alpha/routes.py")))
        self.assertFalse(brain._is_user_tab_source_path(str(self.root / "data/settings.json")))
        self.assertEqual(memory_tools._resolve_repo_path("custom-tabs/alpha/routes.py"), str(self.users / "alpha/routes.py"))
        self.assertIn("alpha/", memory_tools.list_repo_directory("custom-tabs"))
        for name in ("custom-tabs/alpha/../../settings.json", "custom-tabs/../settings.json", "custom-tabs/alpha/../beta/routes.py"):
            with self.assertRaises(ValueError): memory_tools._resolve_repo_path(name)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        _DATA.cleanup()
