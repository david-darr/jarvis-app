"""Fake-driver checks for the contained computer, safety gates and image paths.

No Docker or real subprocess is needed. Live containment lives in
scripts/test_sandbox.py and is run separately when Docker is available.
"""
import asyncio
import ast
import base64
from dataclasses import replace
import json
import io
import os
import re
import shutil
import sys
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
environment = Path(__file__).resolve().parents[1] / (".computer-test-" + uuid.uuid4().hex[:12])
environment.mkdir()
os.environ["JARVIS_DATA_DIR"] = str(environment)

from core import computer, computer_image, permissions, sandbox_browser, tool_access, tool_registry  # noqa: E402
from core.computer_driver import SOURCE  # noqa: E402
from core.providers import native_api, openai_compatible  # noqa: E402
from core.turn_taint import TurnTaint  # noqa: E402
from routes import computer_routes, settings_routes, tool_routes  # noqa: E402
from services.agent_service import agent_service  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nsmall"


class FakeInput:
    def __init__(self, proc):
        self.proc = proc
        self.buffer = b""

    def write(self, data):
        self.buffer += data

    async def drain(self):
        for line in self.buffer.splitlines():
            cmd = json.loads(line)
            self.proc.calls.append(cmd)
            reply = self.proc.reply(cmd)
            self.proc.stdout.feed_data((json.dumps(reply) + "\n").encode())
        self.buffer = b""

    def close(self):
        self.proc.returncode = 0


class FakeProc:
    def __init__(self):
        self.stdin = FakeInput(self)
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode = None
        self.calls = []
        self.url = "about:blank"
        self.element = {}
        self.next_url = None
        self.mouse_clicks = []
        self.window = {"id": "123", "class": "Mousepad", "title": "Untitled"}

    def reply(self, cmd):
        action = cmd["action"]
        result = {"id": cmd["id"]}
        if action in ("inspect", "focused"):
            result["element"] = self.element
        elif action in ("window_at", "focused_window"):
            result["window"] = self.window
        elif action == "done":
            result["done"] = True
        else:
            if action == "open": self.url = cmd["url"]
            elif action == "click":
                if "x" in cmd and "y" in cmd: self.mouse_clicks.append((cmd["x"], cmd["y"]))
                if self.next_url: self.url = self.next_url
            elif action == "desktop_input" and self.next_url: self.url = self.next_url
            elif action == "back": self.url = "https://example.com/"
            result.update(url=self.url, title="Example", screenshot=base64.b64encode(PNG).decode())
            if action == "read":
                result.update(text="Example Domain", elements=[{"ref": "1", "tag": "a", "label": "More", "href": "https://other.com/"}])
            if action == "windows": result["windows"] = [{**self.window, "geometry": {"x": 0, "y": 0, "width": 800, "height": 600}}]
        return result

    async def wait(self):
        return self.returncode or 0

    def kill(self):
        self.returncode = -9


class DriverDesktopTests(unittest.IsolatedAsyncioTestCase):
    def driver(self, names):
        module = ast.parse(SOURCE)
        functions = [node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        namespace = {"asyncio": asyncio, "re": re}
        exec(compile(ast.Module(body=functions, type_ignores=[]), "desktop driver functions", "exec"), namespace)
        return namespace

    def test_libreoffice_seed_uses_installed_product_version(self):
        driver = self.driver({"seed_libreoffice"})
        driver.update(Path=Path, ET=ET)
        home = environment / "office-new"
        registry = environment / "office-registry"
        registry.mkdir()
        (registry / "main.xcd").write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">'
            '<oor:component-schema oor:name="Setup" oor:package="org.openoffice"><component>'
            '<group oor:name="Product"><prop oor:name="ooSetupVersion"><value/></prop></group>'
            '</component></oor:component-schema></oor:data>', encoding="utf-8")
        (registry / "brand.xcd").write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">'
            '<oor:component-data oor:name="Setup" oor:package="org.openoffice"><node oor:name="Product">'
            '<prop oor:name="ooSetupVersionAboutBox"><value>24.2.7.2</value></prop>'
            '<prop oor:name="ooSetupVersion"><value>24.2</value></prop></node></oor:component-data></oor:data>',
            encoding="utf-8")
        with patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
            driver["seed_libreoffice"](registry=registry)
        config = home / ".config/libreoffice/4/user/registrymodifications.xcu"
        name = "{http://openoffice.org/2001/registry}"
        settings = {(item.get(name + "path"), prop.get(name + "name")): prop.findtext("value")
                    for item in ET.parse(config).getroot() for prop in item}
        self.assertEqual(settings, {
            ("/org.openoffice.Office.Common/Misc", "ShowTipOfTheDay"): "false",
            ("/org.openoffice.Office.Common/Misc", "FirstRun"): "false",
            ("/org.openoffice.Setup/Product", "ooSetupLastVersion"): "24.2"})

    def test_libreoffice_seed_preserves_existing_profile(self):
        driver = self.driver({"seed_libreoffice"})
        driver.update(Path=Path, ET=ET)
        home = environment / "office-kept"
        config = home / ".config/libreoffice/4/user/registrymodifications.xcu"
        config.parent.mkdir(parents=True)
        original = b'<person-settings>keep my tips</person-settings>\n'
        config.write_bytes(original)
        with patch.object(ET, "parse", side_effect=AssertionError("existing profiles need no version lookup")):
            driver["seed_libreoffice"](home=home, registry=environment / "absent-registry")
        self.assertEqual(config.read_bytes(), original)

    def test_browser_launch_options_for_desktop_and_headless(self):
        driver = self.driver({"browser_launch_options"})
        driver["PROXY"] = "http://proxy:8080"
        desktop = driver["browser_launch_options"](True)
        self.assertFalse(desktop["headless"])
        self.assertTrue(desktop["no_viewport"])
        self.assertNotIn("viewport", desktop)
        self.assertIn("--window-position=0,0", desktop["args"])
        self.assertIn("--window-size=1280,800", desktop["args"])
        headless = driver["browser_launch_options"](False)
        self.assertTrue(headless["headless"])
        self.assertEqual(headless["viewport"], {"width": 1280, "height": 800})
        self.assertNotIn("no_viewport", headless)
        self.assertFalse(any(arg.startswith("--window-") for arg in headless["args"]))
        for options in (desktop, headless):
            self.assertEqual(options["proxy"], {"server": "http://proxy:8080", "bypass": ""})
            self.assertIn("--proxy-bypass-list=<-loopback>", options["args"])
            self.assertFalse(options["accept_downloads"])

    async def test_driver_rechecks_browser_and_changed_window_before_input(self):
        driver = self.driver({"desktop_input", "browser_window", "point"})
        window = {"id": "123", "class": "Chromium", "title": "Browser"}
        driver.update(window_at=AsyncMock(return_value=window), focused_window=AsyncMock(return_value=window),
                      xdo=AsyncMock(), BROWSER_REFUSAL="That is the browser window")
        for kind in ("click", "type", "key", "scroll"):
            with self.assertRaisesRegex(ValueError, "browser window"):
                await driver["desktop_input"]({"kind": kind, "x": 10, "y": 20, "text": "secret", "key": "Enter"})
        driver["xdo"].assert_not_awaited()
        window["class"] = "Mousepad"
        with self.assertRaisesRegex(ValueError, "window changed"):
            await driver["desktop_input"]({"kind": "type", "text": "secret", "expected_window": {**window, "id": "456"}})
        driver["xdo"].assert_not_awaited()
        window["class"] = ""
        with self.assertRaisesRegex(ValueError, "could not identify"):
            await driver["desktop_input"]({"kind": "click", "x": 10, "y": 20, "expected_window": window})

    async def test_driver_person_input_and_key_translation(self):
        driver = self.driver({"desktop_input", "browser_window", "point"})
        window = {"id": "123", "class": "Chromium", "title": "Browser"}
        driver.update(window_at=AsyncMock(return_value=window), focused_window=AsyncMock(return_value=window),
                      xdo=AsyncMock(), BROWSER_REFUSAL="browser window")
        for kind, values in (("click", {"x": 10, "y": 20}), ("type", {"text": "--private"}),
                             ("key", {"key": "Control+Enter"}), ("scroll", {"dy": 200})):
            await driver["desktop_input"]({"kind": kind, "person": True, **values})
        calls = [call.args for call in driver["xdo"].await_args_list]
        self.assertIn(('type', '--clearmodifiers', '--delay', '0', '--', '--private'), calls)
        self.assertIn(('key', '--clearmodifiers', 'ctrl+Return'), calls)
        for key in ("exec", "exec sh", "--window 123", "a\nexec"):
            with self.assertRaises(ValueError):
                await driver["desktop_input"]({"kind": "key", "person": True, "key": key})

    async def test_driver_launch_allowlist_and_window_reply_preserve_protocol_id(self):
        driver = self.driver({"main", "browser_launch_options"})
        class Playwright:
            async def __aenter__(self): return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
            async def __aexit__(self, *args): pass
        page = SimpleNamespace(url="about:blank", title=AsyncMock(return_value=""))
        context = SimpleNamespace(pages=[page], add_init_script=AsyncMock(), close=AsyncMock())
        launch = AsyncMock(return_value=context)
        commands = [{"id": 1, "action": "launch", "app": "sh"}, {"id": 2, "action": "window_at", "x": 10, "y": 20},
                    {"id": 3, "action": "done"}]
        replies = []
        driver.update(DESKTOP=True, PROXY="proxy", DOTS="private script", APPS=computer_image.DESKTOP_APPS,
                      MAX_FPS=30, time=__import__("time"), json=json, sys=SimpleNamespace(stdin=io.StringIO(
                          "".join(json.dumps(cmd) + "\n" for cmd in commands))),
                      async_playwright=Playwright, start_desktop=AsyncMock(), emit=replies.append,
                      window_at=AsyncMock(return_value={"id": "123", "class": "Mousepad", "title": "Editor"}))
        with patch.object(asyncio, "create_subprocess_exec", new=AsyncMock()) as spawn:
            await driver["main"]()
        spawn.assert_not_awaited()
        self.assertIn("choose files", replies[0]["error"])
        self.assertEqual(replies[1]["id"], 2)
        self.assertEqual(replies[1]["window"]["id"], "123")
        self.assertFalse(launch.await_args.kwargs["headless"])
        self.assertIn('--class=kairos-computer-browser', launch.await_args.kwargs["args"])
        context.add_init_script.assert_awaited_once_with(script="private script")


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_recipe_pins_and_separate_tag(self):
        recipe = computer_image.desktop_dockerfile()
        self.assertTrue(recipe.startswith(f"FROM {sandbox_browser.BROWSER_IMAGE}\n"))
        for value in ("https://snapshot.ubuntu.com/ubuntu/20261007T000000Z", "noble noble-updates noble-security",
                      "openbox xdotool pcmanfm mousepad atril ristretto",
                      "libreoffice-writer libreoffice-calc libreoffice-impress fonts-dejavu-core",
                      "--no-install-recommends", "--require-hashes", "--only-binary=:all:", "pillow==12.3.0",
                      "78cb2c6865a35ab8ff8b75fd122f6033b92a62c82801110e48ddd6c936a45d91",
                      "d9c7f76c0673154f044e9d78c8655fb4213f6ca31a836df48b40fe5d187717b9"):
            self.assertIn(value, recipe)
        for requirement in computer_image.REQUIREMENTS: self.assertIn(requirement, recipe)
        self.assertNotIn("pillow", computer_image.DOCKERFILE)
        self.assertTrue(computer_image.image_tag(desktop=True).startswith("kairos-computer-desktop:"))
        self.assertNotEqual(computer_image.image_tag(desktop=True), computer_image.image_tag())
        self.assertIsNot(computer_image._lock, computer_image._desktop_lock)

    async def test_desktop_build_uses_its_recipe_and_timeout(self):
        proc = AsyncMock(returncode=0)
        proc.communicate.return_value = (b"", b"")
        with patch.object(computer_image.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)), \
             patch.object(computer_image.asyncio, "wait_for", wraps=asyncio.wait_for) as wait:
            await computer_image._build(computer_image.image_tag(desktop=True), desktop=True)
        proc.communicate.assert_awaited_once_with(computer_image.desktop_dockerfile().encode())
        self.assertEqual(wait.call_args.args[1], 900)
        with patch.object(computer_image, "image_ready", new=AsyncMock(return_value=False)) as ready, \
             patch.object(computer_image, "_build", new=AsyncMock()) as build:
            self.assertEqual(await computer_image.ensure_image(desktop=True), computer_image.image_tag(desktop=True))
        ready.assert_awaited_once_with(desktop=True)
        build.assert_awaited_once_with(computer_image.image_tag(desktop=True), desktop=True)

    async def test_dockerfile_is_pinned_and_hash_checked(self):
        recipe = computer_image.DOCKERFILE
        self.assertTrue(recipe.startswith(f"FROM {sandbox_browser.BROWSER_IMAGE}\n"))
        self.assertIn("--require-hashes", recipe)
        self.assertIn("--only-binary=:all:", recipe)
        for digest in (
            "ad21bc07516b187965a7521c5cf0df0bd657b17482eaad74335272d35a2b07de",
            "354e15b29503565fc598b89f16fbe070459343bef9d7498a93e304864000c6a7",
            "975736b002ed080d124cf81a79cb7e05cb26d6b3f5c7a7b651c0fcce70353aa1",
            "e85880b538e59a59f55117b81f208a6660ad5ac328aad9305f812d9b8bc67a0f",
            "af2f8fede4171ef667dfded53f96e2ed0d6e6bd7ee3bb46437f77e3b57689228",
            "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
        ):
            self.assertIn("sha256:" + digest, recipe)
        self.assertNotEqual(computer_image.image_tag(recipe), computer_image.image_tag(recipe + "# changed\n"))

    async def test_existing_image_is_not_rebuilt(self):
        with patch.object(computer_image.sandbox, "_docker", new=AsyncMock(return_value=(0, "[]", ""))) as docker, \
             patch.object(computer_image, "_build", new=AsyncMock()) as build:
            self.assertEqual(await computer_image.ensure_image(), computer_image.image_tag())
        docker.assert_awaited_once_with("image", "inspect", computer_image.image_tag())
        build.assert_not_awaited()

    async def test_two_first_uses_build_once(self):
        ready = False
        async def inspect(*args):
            self.assertEqual(args, ("image", "inspect", computer_image.image_tag()))
            return (0, "[]", "") if ready else (1, "", "missing")
        async def build(tag):
            nonlocal ready
            self.assertEqual(tag, computer_image.image_tag())
            await asyncio.sleep(0)
            ready = True
        with patch.object(computer_image.sandbox, "_docker", side_effect=inspect), \
             patch.object(computer_image, "_build", side_effect=build) as builder:
            tags = await asyncio.gather(computer_image.ensure_image(), computer_image.ensure_image())
        self.assertEqual(tags, [computer_image.image_tag()] * 2)
        builder.assert_awaited_once()

    async def test_build_streams_dockerfile_and_reports_failure(self):
        class BuildProc:
            returncode = 1
            payload = None

            async def communicate(self, payload):
                self.payload = payload
                return b"", b"pip could not install the wheel\n"

        proc = BuildProc()
        with patch.object(computer_image.sandbox, "_docker", new=AsyncMock(return_value=(1, "", "missing"))), \
             patch.object(computer_image.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
            with self.assertRaisesRegex(computer.sandbox.SandboxUnavailable,
                                        "couldn't prepare the contained computer: pip could not install the wheel"):
                await computer_image.ensure_image()
        self.assertEqual(spawn.await_args.args, ("docker", "build", "-t", computer_image.image_tag(), "-"))
        self.assertEqual(proc.payload, computer_image.DOCKERFILE.encode())


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.procs = []
        self.argv = []

        async def factory(*args, **kwargs):
            self.argv.append(args)
            proc = FakeProc()
            self.procs.append(proc)
            return proc

        self.manager = computer.ComputerManager(factory, clock=lambda: self.now)
        self.available = patch("core.computer.sandbox.available", new=AsyncMock(return_value=(True, "")))
        self.egress = patch("core.computer.sandbox_egress.ensure", new=AsyncMock())
        self.image = patch("core.computer.computer_image.ensure_image", new=AsyncMock(return_value="kairos-computer:fake"))
        self.decide = patch("core.computer.permissions.decide", new=AsyncMock(return_value=permissions.Decision("allow")))
        self.available.start(); self.egress.start(); self.image.start(); self.ask = self.decide.start()
        self.ctx = tool_registry.ToolContext(session_id="s1", is_admin=True, turn_taint=TurnTaint())

    async def asyncTearDown(self):
        await self.manager.close_all()
        self.available.stop(); self.egress.stop(); self.image.stop(); self.decide.stop()

    async def open(self, owner="chat:s1", ctx=None):
        return await self.manager.act(owner, "open", {"url": "https://example.com/"}, ctx or self.ctx)

    async def desktop(self):
        with patch("core.computer._desktop_enabled", return_value=True):
            await self.open()
        return self.manager._computers["chat:s1"]

    async def test_desktop_mode_is_fixed_at_start_and_reports_hardening(self):
        await self.desktop()
        self.assertTrue(self.manager.running()[0]["desktop"])
        self.assertIn("KAIROS_COMPUTER_DESKTOP=1", self.argv[0])
        self.assertEqual(self.argv[0][self.argv[0].index("--memory") + 1], "2g")
        self.assertIn("--read-only", self.argv[0])
        self.assertNotIn("--mount", self.argv[0])
        with patch("core.computer._desktop_enabled", return_value=False):
            result = await self.manager.act("chat:s1", "screenshot", {}, self.ctx)
            self.assertIn("Desktop screenshot", result["text"])
            self.assertEqual(self.procs[0].calls[-1]["action"], "desktop_screenshot")
            await self.manager.stop("chat:s1")
            await self.open()
        self.assertFalse(self.manager.running()[0]["desktop"])
        self.assertEqual(self.argv[1][self.argv[1].index("--memory") + 1], "1g")
        with patch("core.computer._desktop_enabled", return_value=True):
            self.assertIn("desktop is off", (await self.manager.act("chat:s1", "launch", {"app": "editor"}, self.ctx))["text"])

    async def test_desktop_launch_allowlist_and_windows(self):
        await self.desktop()
        for app in ("terminal", "mail", "chat", "sh", "mousepad; sh", "../../bin/sh", ""):
            count = len(self.procs[0].calls)
            result = await self.manager.act("chat:s1", "launch", {"app": app}, self.ctx)
            self.assertIn("Not run", result["text"])
            self.assertFalse(any(c["action"] == "launch" for c in self.procs[0].calls[count:]))
        for app in computer_image.DESKTOP_APPS:
            await self.manager.act("chat:s1", "launch", {"app": app}, self.ctx)
            self.assertEqual(self.procs[0].calls[-1]["app"], app)
        result = await self.manager.act("chat:s1", "windows", {}, self.ctx)
        self.assertIn('"class": "Mousepad"', result["text"])
        self.assertIn('"geometry"', result["text"])

    async def test_model_desktop_input_refuses_browser_and_unknown_windows(self):
        await self.desktop()
        proc = self.procs[0]
        for cls in ("Chromium", "Google-chrome", "kairos-computer-browser", ""):
            proc.window["class"] = cls
            for action, values in (("click", {"x": 20, "y": 30}), ("type", {"text": "private"}),
                                   ("key", {"key": "Enter"}), ("scroll", {"dy": 100})):
                count = len(proc.calls)
                result = await self.manager.act("chat:s1", action, {"desktop": True, **values}, self.ctx)
                self.assertTrue(result["text"].startswith("Not run:"))
                self.assertFalse(any(c["action"] == "desktop_input" for c in proc.calls[count:]))
                self.assertEqual(proc.calls[-1]["action"], "window_at" if action in ("click", "scroll") else "focused_window")
                if cls: self.assertIn("That is the browser window: use the computer's web actions", result["text"])
        proc.window["class"] = "Mousepad"
        await self.manager.act("chat:s1", "click", {"desktop": True, "x": 20, "y": 30}, self.ctx)
        self.assertEqual(proc.calls[-1]["action"], "desktop_input")
        self.assertEqual(proc.calls[-1]["expected_window"], proc.window)
        self.assertNotIn("person", proc.calls[-1])

    async def test_person_desktop_input_goes_to_browser_and_desktop(self):
        await self.desktop()
        self.manager.takeover("chat:s1")
        for cls in ("Chromium", "Mousepad", ""):
            self.procs[0].window["class"] = cls
            for kind, values in (("click", {"x": 10, "y": 20}), ("type", {"text": "private"}),
                                 ("key", {"key": "Enter"}), ("scroll", {"dy": 100})):
                await self.manager.person_input("chat:s1", kind, values)
                command = self.procs[0].calls[-1]
                self.assertEqual(command["action"], "desktop_input")
                self.assertEqual(command["kind"], kind)
                self.assertTrue(command["person"])
                self.assertNotIn("expected_window", command)

    async def test_desktop_off_refuses_without_starting_and_non_admin_stays_browser(self):
        with patch("core.computer._desktop_enabled", return_value=False):
            for action, values in (("launch", {"app": "editor"}), ("windows", {}), ("click", {"desktop": True}),
                                   ("screenshot", {"desktop": True})):
                self.assertIn("desktop is off", (await self.manager.act("chat:s1", action, values, self.ctx))["text"])
        self.assertEqual(self.argv, [])
        ctx = tool_registry.ToolContext(session_id="s1", is_admin=False)
        with patch("core.computer._desktop_enabled", return_value=True):
            self.assertIn("desktop is off", (await self.manager.act("chat:s1", "windows", {}, ctx))["text"])
            await self.open(ctx=ctx)
        self.assertFalse(self.manager.running()[0]["desktop"])

    async def test_web_guards_and_browser_screenshot_still_apply_with_desktop(self):
        await self.desktop()
        self.procs[0].element = {"tag": "button", "signals": ["Place order"], "label": "Place order"}
        result = await self.manager.act("chat:s1", "click", {"ref": "1"}, self.ctx)
        self.assertIn("Stopped before pressing 'Place order'", result["text"])
        self.assertFalse(any(c["action"] in ("click", "desktop_input") for c in self.procs[0].calls))
        await self.manager.act("chat:s1", "screenshot", {"desktop": False}, self.ctx)
        self.assertEqual(self.procs[0].calls[-1]["action"], "screenshot")

    async def test_desktop_keeps_site_checks_before_and_after_input(self):
        await self.desktop()
        self.procs[0].next_url = "https://other.com/"
        self.ask.return_value = permissions.Decision("deny", "No")
        result = await self.manager.act("chat:s1", "click", {"desktop": True, "x": 20, "y": 30}, self.ctx)
        self.assertIn("Not opened", result["text"])
        self.assertEqual(result["images"], [])
        self.assertEqual(self.procs[0].calls[-1]["action"], "back")
        sent = next(cmd for cmd in self.procs[0].calls if cmd["action"] == "desktop_input")
        self.assertEqual(sent["expected_url"], "https://example.com/")
        self.procs[0].url = "https://third.com/"
        count = len(self.procs[0].calls)
        result = await self.manager.act("chat:s1", "screenshot", {}, self.ctx)
        self.assertIn("Not opened", result["text"])
        self.assertFalse(any(cmd["action"] == "desktop_screenshot" for cmd in self.procs[0].calls[count:]))

    async def test_protocol_taint_read_and_done(self):
        result = await self.open()
        self.assertEqual(result["images"], [PNG])
        self.assertTrue(self.ctx.turn_taint.tainted)
        read = await self.manager.act("chat:s1", "read", {}, self.ctx)
        self.assertIn("[1] a More", read["text"])
        self.assertEqual([c["id"] for c in self.procs[0].calls], [1, 2, 3])
        self.assertEqual(self.procs[0].calls[0]["action"], "open")
        self.assertEqual((await self.manager.act("chat:s1", "done", {}, self.ctx))["images"], [])
        self.assertEqual(self.manager.running(), [])

    async def test_closed_frame_is_retained_expires_and_chat_delete_forgets_it(self):
        from core.session_manager import session_manager
        await self.open()
        self.manager._computers["chat:s1"].screenshot = (Path(__file__).resolve().parents[1] / "static/img/computer-fixture.jpg").read_bytes()
        await self.manager.act("chat:s1", "done", {}, self.ctx)
        saved = self.manager.last("chat:s1")
        self.assertEqual(saved["url"], "https://example.com/")
        self.assertTrue(saved["closed_at"])
        self.assertTrue(base64.b64decode(saved["image"]).startswith(b"\xff\xd8"))
        self.now += 601
        self.assertIsNone(self.manager.last("chat:s1"))
        await self.open()
        await self.manager.stop("chat:s1")
        self.assertIsNotNone(self.manager.last("chat:s1"))
        with patch.object(computer, "manager", self.manager), patch("core.session_manager.store.delete_session"):
            session_manager.delete_session("s1")
        self.assertIsNone(self.manager.last("chat:s1"))

    async def test_new_host_asked_once_and_denial_goes_back(self):
        await self.open()
        self.procs[0].next_url = "https://other.com/"
        self.ask.return_value = permissions.Decision("deny", "No")
        result = await self.manager.act("chat:s1", "click", {"ref": "1"}, self.ctx)
        self.assertIn("Not opened", result["text"])
        self.assertEqual(self.procs[0].calls[-1]["action"], "back")
        self.assertEqual([c.kwargs["target"] for c in self.ask.call_args_list], ["example.com", "other.com"])

    async def test_idle_redirect_is_checked_before_reading(self):
        await self.open()
        self.procs[0].url = "https://other.com/"
        self.ask.return_value = permissions.Decision("deny", "No")
        result = await self.manager.act("chat:s1", "read", {}, self.ctx)
        self.assertIn("Not opened", result["text"])
        self.assertNotIn("read", [c["action"] for c in self.procs[0].calls])

    async def test_buy_send_post_categories_hard_stop_without_decision(self):
        await self.open()
        # The whole-word rule leaves "Postcode" alone; repost and retweet are posting.
        labels = ("Buy now", "Pay", "Purchase", "Order", "Place order", "Checkout", "Check out",
                  "Subscribe", "Donate", "Transfer", "Send", "Reply", "Forward", "Post", "Repost",
                  "Retweet", "Publish", "Share", "Comment", "Submit", "Confirm")
        for label in labels:
            self.ask.reset_mock()
            self.procs[0].element = {"tag": "button", "label": label, "signals": [label], "submit": True}
            result = await self.manager.act("chat:s1", "click", {"ref": "1"}, self.ctx)
            self.assertEqual(result["text"],
                f"Stopped before pressing '{label}' on example.com: buying, sending and posting are done by the person. "
                "Everything up to it is ready; ask them to take over and press it.")
            self.ask.assert_not_called()
        self.assertNotIn("click", [c["action"] for c in self.procs[0].calls])

    async def test_all_element_signals_are_checked(self):
        await self.open()
        self.ask.reset_mock()
        self.procs[0].element = {"tag": "button", "label": "Continue", "signals": ["Continue", "Pay now"]}
        result = await self.manager.act("chat:s1", "click", {"ref": "1"}, self.ctx)
        self.assertIn("pressing 'Pay now'", result["text"])
        self.ask.assert_not_called()
        long_label = "Continue " + ("x" * 200) + " send"
        self.procs[0].element = {"tag": "button", "label": "Continue", "signals": [long_label]}
        result = await self.manager.act("chat:s1", "click", {"ref": "1"}, self.ctx)
        self.assertIn("Stopped before pressing", result["text"])
        self.ask.assert_not_called()

    async def test_enter_checks_form_submit_but_search_proceeds(self):
        await self.open()
        self.ask.reset_mock()
        self.procs[0].element = {"tag": "input", "label": "Search", "form": True,
                                 "form_submits": [{"label": "Place order", "signals": ["Place order"]}]}
        stopped = await self.manager.act("chat:s1", "key", {"key": "Control+Enter"}, self.ctx)
        self.assertIn("pressing 'Place order'", stopped["text"])
        self.assertNotIn("key", [c["action"] for c in self.procs[0].calls])
        self.ask.assert_not_called()
        self.procs[0].element["form_submits"] = [{"label": "Go", "signals": ["Go"]}]
        await self.manager.act("chat:s1", "key", {"key": "Enter"}, self.ctx)
        self.assertEqual(self.procs[0].calls[-1]["action"], "key")
        self.ask.assert_not_called()

    async def test_search_and_whole_word_nonmatches_proceed(self):
        await self.open()
        self.ask.reset_mock()
        for label in ("Search", "Postcode", "Likely matches", "Followers", "Starred items"):
            self.procs[0].element = {"tag": "button", "label": label, "signals": [label], "submit": True}
            await self.manager.act("chat:s1", "click", {"ref": "1"}, self.ctx)
            self.assertEqual(self.procs[0].calls[-1]["action"], "click", label)
        self.ask.assert_not_called()

    async def test_reactions_stop_by_default_and_an_admin_can_allow_them(self):
        await self.open()
        self.ask.reset_mock()
        labels = ("like this video along with 1,234 other people", "Unlike", "Follow", "Unfollow",
                  "Upvote", "Downvote", "React", "Favourite", "Favorite", "Star", "Heart")
        with patch("core.computer._reactions_allowed", return_value=False):
            for label in labels:
                self.procs[0].element = {"tag": "button", "label": label, "signals": [label]}
                result = await self.manager.act("chat:s1", "click", {"ref": "1"}, self.ctx)
                self.assertTrue(result["text"].startswith(f"Stopped before pressing '{label}' on example.com: "
                                                          "likes, follows and reactions are done by the person"), label)
        self.assertNotIn("click", [c["action"] for c in self.procs[0].calls])
        with patch("core.computer._reactions_allowed", return_value=True):
            for label in ("Like", "Follow"):
                self.procs[0].element = {"tag": "button", "label": label, "signals": [label]}
                await self.manager.act("chat:s1", "click", {"ref": "1"}, self.ctx)
                self.assertEqual(self.procs[0].calls[-1]["action"], "click", label)
            # Allowing reactions never relaxes posting.
            self.procs[0].element = {"tag": "button", "label": "Repost", "signals": ["Repost"]}
            result = await self.manager.act("chat:s1", "click", {"ref": "1"}, self.ctx)
            self.assertIn("buying, sending and posting are done by the person", result["text"])
        self.ask.assert_not_called()

    async def test_reaction_stop_reaches_an_agents_inbox(self):
        from services.agent_service import agent_service
        agent = agent_service.create("Reaction check")
        try:
            owner = "agent:" + agent["id"]
            ctx = tool_registry.ToolContext(agent_id=agent["id"], is_admin=True, turn_taint=TurnTaint())
            await self.open(owner, ctx)
            self.procs[-1].element = {"tag": "button", "label": "Like", "signals": ["Like"]}
            with patch("core.computer._reactions_allowed", return_value=False):
                await self.manager.act(owner, "click", {"ref": "1"}, ctx)
            self.assertEqual(agent_service.inbox(agent["id"])[0]["title"], "Ready for you: press 'Like' at example.com")
        finally:
            agent_service.delete(agent["id"])

    async def test_delete_asks_once_or_reject_and_auto_allows(self):
        await self.open()
        self.ask.reset_mock()
        self.ask.return_value = permissions.Decision("allow", "Allowed by Auto")
        for label in ("Delete item", "Remove item"):
            self.procs[0].element = {"tag": "button", "label": label, "signals": [label]}
            result = await self.manager.act("chat:s1", "click", {"ref": "1"}, self.ctx)
            self.assertIn("click completed", result["text"])
            self.assertEqual(self.ask.call_args.kwargs["tool"], "computer_delete")
            self.assertEqual([choice["id"] for choice in self.ask.call_args.kwargs["choices"]], ["once", "reject"])
            self.assertEqual(self.procs[0].calls[-1]["action"], "click")

    async def test_agent_ready_to_press_inbox(self):
        agent = agent_service.create("Ready Agent")
        ctx = tool_registry.ToolContext(agent_id=agent["id"], is_admin=True)
        owner = "agent:" + agent["id"]
        await self.open(owner, ctx)
        self.ask.reset_mock()
        self.procs[0].element = {"tag": "button", "label": "Send message", "signals": ["Send message"]}
        result = await self.manager.act(owner, "click", {"ref": "1"}, ctx)
        self.assertIn("Stopped before pressing", result["text"])
        self.ask.assert_not_called()
        item = agent_service.inbox(agent["id"])[0]
        self.assertEqual(item["title"], "Ready for you: press 'Send message' at example.com")
        self.assertIn("https://example.com/", item["body"])
        await self.manager.stop(owner)
        agent_service.delete(agent["id"])

    async def test_private_fields_refused_for_agents_even_in_auto(self):
        agent = agent_service.create("Computer Agent")
        ctx = tool_registry.ToolContext(agent_id=agent["id"], is_admin=True)
        owner = "agent:" + agent["id"]
        await self.open(owner, ctx)
        for info in ({"tag": "input", "type": "password"}, {"tag": "input", "autocomplete": "current-password"},
                     {"tag": "input", "autocomplete": "new-password"},
                     {"tag": "input", "autocomplete": "one-time-code"},
                     {"tag": "input", "autocomplete": "cc-number"}):
            self.procs[0].element = info
            result = await self.manager.act(owner, "type", {"text": "secret", "ref": "1"}, ctx)
            self.assertIn("Not typed", result["text"])
        self.assertEqual(len([c for c in self.procs[0].calls if c["action"] == "type"]), 0)
        self.procs[0].element = {"tag": "input", "type": "password"}
        keyed = await self.manager.act(owner, "key", {"key": "A"}, ctx)
        self.assertIn("Not typed", keyed["text"])
        self.assertEqual(len([c for c in self.procs[0].calls if c["action"] == "key"]), 0)
        self.assertEqual(agent_service.inbox(agent["id"])[0]["kind"], "question")
        self.assertIn("example.com", agent_service.inbox(agent["id"])[0]["title"])
        await self.manager.stop(owner)
        agent_service.delete(agent["id"])

    async def test_opaque_frame_stops_input_and_notifies_agent(self):
        agent = agent_service.create("Frame Agent")
        owner = "agent:" + agent["id"]
        ctx = tool_registry.ToolContext(agent_id=agent["id"], is_admin=True)
        await self.open(owner, ctx)
        self.ask.reset_mock()
        self.procs[0].element = {"opaque_frame": True, "tag": "iframe"}
        typed = await self.manager.act(owner, "type", {"x": 20, "y": 30, "text": "secret"}, ctx)
        keyed = await self.manager.act(owner, "key", {"key": "Enter"}, ctx)
        clicked = await self.manager.act(owner, "click", {"x": 20, "y": 30}, ctx)
        self.assertIn("Ask the person to take over", typed["text"])
        self.assertIn("Ask the person to take over", keyed["text"])
        self.assertEqual(clicked["text"], computer._opaque_frame_message())
        self.ask.assert_not_called()
        self.assertFalse(any(c["action"] in ("type", "key", "click") for c in self.procs[0].calls))
        self.assertIn("embedded frame", agent_service.inbox(agent["id"])[0]["title"])
        self.assertIn("https://example.com/", agent_service.inbox(agent["id"])[0]["body"])
        await self.manager.stop(owner)
        agent_service.delete(agent["id"])

    async def test_frame_resolved_payment_field_and_pay_button_stop(self):
        await self.open()
        self.ask.reset_mock()
        self.procs[0].element = {"tag": "input", "autocomplete": "cc-number", "frame_depth": 1}
        typed = await self.manager.act("chat:s1", "type", {"x": 80, "y": 90, "text": "4111"}, self.ctx)
        self.assertIn("Not typed", typed["text"])
        self.procs[0].element = {"tag": "button", "label": "Pay", "signals": ["Pay"], "frame_depth": 1}
        clicked = await self.manager.act("chat:s1", "click", {"x": 80, "y": 90}, self.ctx)
        self.assertIn("Stopped before pressing 'Pay'", clicked["text"])
        self.ask.assert_not_called()
        self.assertFalse(any(c["action"] in ("type", "click") for c in self.procs[0].calls))

    async def test_coordinate_click_keeps_inspected_point(self):
        await self.open()
        self.procs[0].element = {"tag": "button", "label": "Search", "signals": ["Search"]}
        await self.manager.act("chat:s1", "click", {"x": 97, "y": 238}, self.ctx)
        inspect = next(c for c in self.procs[0].calls if c["action"] == "inspect")
        clicked = self.procs[0].calls[-1]
        self.assertEqual((inspect["x"], inspect["y"]), (97, 238))
        self.assertEqual((clicked["x"], clicked["y"]), (97, 238))
        self.assertEqual(self.procs[0].mouse_clicks, [(97, 238)])

    async def test_proxy_address_passed_to_driver(self):
        await self.open()
        self.assertIn(f"KAIROS_COMPUTER_PROXY={computer.sandbox_egress.PROXY_URL}", self.argv[0])
        self.assertIn("kairos-computer:fake", self.argv[0])

    async def test_takeover_waits_for_handback_and_times_out(self):
        await self.open()
        self.assertTrue(self.manager.takeover("chat:s1"))
        waiting = asyncio.create_task(self.manager.act("chat:s1", "read", {}, self.ctx))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        self.assertTrue(self.manager.running()[0]["waiting_model"])
        self.manager.hand_back("chat:s1")
        self.assertIn("read completed", (await waiting)["text"])
        self.manager.takeover("chat:s1")
        waiting = asyncio.create_task(self.manager.act("chat:s1", "read", {}, self.ctx))
        await asyncio.sleep(0)
        self.now += 601
        self.assertEqual((await asyncio.wait_for(waiting, 1))["text"],
                         "Not run: the person has control of the computer.")

    async def test_person_input_requires_takeover_and_skips_model_checks(self):
        await self.open()
        with self.assertRaises(ValueError):
            await self.manager.person_input("chat:s1", "type", {"text": "private value"})
        self.manager.takeover("chat:s1")
        self.procs[0].element = {"opaque_frame": True, "tag": "iframe"}
        await self.manager.person_input("chat:s1", "type", {"text": "private value"})
        command = self.procs[0].calls[-1]
        self.assertEqual(command["action"], "person_type")
        self.assertNotIn("expected", command)
        self.assertNotIn("private value", str(self.manager.running()))

    async def test_watched_frames_are_throttled(self):
        await self.open()
        first, second = await asyncio.gather(self.manager.frame("chat:s1"), self.manager.frame("chat:s1"))
        self.assertEqual(first["image"], second["image"])
        self.assertEqual(len([c for c in self.procs[0].calls if c["action"] == "snapshot"]), 1)
        self.now += 0.5
        await self.manager.frame("chat:s1")
        self.assertEqual(len([c for c in self.procs[0].calls if c["action"] == "snapshot"]), 2)


    async def test_idle_and_lru_cap(self):
        await self.open()
        self.now += 1
        await self.open("chat:s2")
        self.now += 1
        await self.open("chat:s3")
        self.assertEqual({r["owner"] for r in self.manager.running()}, {"chat:s2", "chat:s3"})
        self.assertEqual(self.procs[0].returncode, 0)
        self.now += computer.IDLE_SECONDS
        await self.manager._close_idle()
        self.assertEqual(self.manager.running(), [])

    async def test_profile_mount_only_for_opted_in_agent(self):
        await self.open()
        self.assertNotIn("--mount", self.argv[0])
        agent = agent_service.create("Profile Agent")
        ctx = tool_registry.ToolContext(agent_id=agent["id"], is_admin=True)
        await self.open("agent:" + agent["id"], ctx)
        self.assertNotIn("--mount", self.argv[1])
        await self.manager.stop("agent:" + agent["id"])
        agent_service.update(agent["id"], keep_signed_in=True)
        await self.open("agent:" + agent["id"], ctx)
        mounted = self.argv[2]
        self.assertEqual(mounted.count("--mount"), 1)
        self.assertIn(str(computer.PROFILES / agent["id"]), " ".join(mounted))
        with patch.object(computer, "manager", self.manager):
            with self.assertRaises(ValueError): agent_service.forget_logins(agent["id"])
            await self.manager.stop("agent:" + agent["id"])
            agent_service.forget_logins(agent["id"])
            self.assertFalse((computer.PROFILES / agent["id"]).exists())
            agent_service.delete(agent["id"])


class RouteTests(unittest.TestCase):
    def test_desktop_setting_round_trips_and_defaults_off(self):
        from core import settings
        self.app.include_router(settings_routes.router)
        self.app.dependency_overrides[settings_routes.require_admin] = lambda: "alice"
        saved = settings.get_setting("computer_use")
        try:
            self.assertFalse(settings.DEFAULTS["computer_use"]["desktop"])
            for desktop in (True, False):
                value = {"enabled": True, "allow_non_admins": False, "allow_reactions": False, "desktop": desktop}
                response = self.client.post("/api/settings/computer-use", json=value)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["computer_use"], value)
                self.assertEqual(self.client.get("/api/settings").json()["computer_use"], value)
            self.assertFalse(self.client.post("/api/settings/computer-use", json={"enabled": True}).json()["computer_use"]["desktop"])
        finally:
            settings.update_settings(computer_use=saved)

    def test_status_checks_desktop_image_without_building(self):
        with patch.object(computer_routes.sandbox, "available", new=AsyncMock(return_value=(True, ""))), \
             patch.object(computer_routes.computer_image, "image_ready", new=AsyncMock(side_effect=[True, False])) as ready, \
             patch.object(computer_routes.computer_image, "ensure_image", new=AsyncMock()) as build, \
             patch.object(computer_routes.agent_service, "list_agents", return_value=[]):
            response = self.client.get("/api/computer/status")
        self.assertTrue(response.json()["image_ready"])
        self.assertFalse(response.json()["desktop_image_ready"])
        self.assertEqual(ready.await_args_list[-1].kwargs, {"desktop": True})
        build.assert_not_called()

    def setUp(self):
        app = FastAPI(); app.include_router(computer_routes.router)
        app.dependency_overrides[computer_routes.require_user] = lambda: "alice"
        app.dependency_overrides[computer_routes.require_admin] = lambda: "alice"
        self.client = TestClient(app)
        self.app = app
        self.rows = [{"owner": owner, "url": "https://example.com/", "title": "Example",
                      "last_action": 1, "taken_over": False, "waiting_model": False}
                     for owner in ("chat:a", "chat:b", "agent:c")]
        self.admin = patch.object(computer_routes.auth_manager, "is_admin", return_value=False)
        self.header = patch.object(computer_routes, "get_session_header",
                                   side_effect=lambda ident: ({"owner_user": "alice" if ident == "a" else "bob"}, 0))
        self.agent = patch.object(computer_routes.agent_service, "get", return_value={"id": "c"})
        self.running = patch.object(computer_routes.computer, "running", return_value=self.rows)
        self.is_running = patch.object(computer_routes.computer.manager, "is_running", return_value=True)
        for item in (self.admin, self.header, self.agent, self.running, self.is_running): item.start()

    def tearDown(self):
        for item in (self.admin, self.header, self.agent, self.running, self.is_running): item.stop()
        self.client.close()

    def test_non_admin_sees_and_controls_only_own_chat(self):
        self.assertEqual([item["owner"] for item in self.client.get("/api/computer").json()], ["chat:a"])
        for owner in ("chat:b", "agent:c"):
            self.assertEqual(self.client.post(f"/api/computer/{owner}/stop").status_code, 403)
            self.assertEqual(self.client.post(f"/api/computer/{owner}/takeover").status_code, 403)
            self.assertEqual(self.client.get(f"/api/computer/{owner}/frames").status_code, 403)
            self.assertEqual(self.client.post(f"/api/computer/{owner}/input", json={"kind": "type", "text": "secret"}).status_code, 403)
        with patch.object(computer_routes.auth_manager, "is_admin", return_value=True):
            self.assertEqual(len(self.client.get("/api/computer").json()), 3)

    def test_input_refused_before_takeover_and_text_is_not_audited_or_logged(self):
        secret = "private value 1234"
        with patch.object(computer_routes.computer.manager, "person_input", new=AsyncMock(
                side_effect=ValueError("take over the computer first"))):
            self.assertEqual(self.client.post("/api/computer/chat:a/input",
                              json={"kind": "type", "text": secret}).status_code, 409)
        with patch.object(computer_routes.computer.manager, "person_input", new=AsyncMock()) as sent, \
             patch("core.permissions.record_tool_use") as audit, \
             patch("logging.Logger._log") as logs:
            response = self.client.post("/api/computer/chat:a/input", json={"kind": "type", "text": secret})
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(sent.await_args.args, ("chat:a", "type", {"text": secret}))
        audit.assert_not_called()
        self.assertNotIn(secret, str(response.json()))
        self.assertNotIn(secret, str(logs.mock_calls))

    def test_last_frame_access_and_absence(self):
        with patch.object(computer_routes.computer.manager, "last", return_value={"owner": "chat:a", "image": "jpeg", "closed_at": 1}):
            result = self.client.get("/api/computer/chat:a/last")
            self.assertEqual(result.json()["image"], "jpeg")
            self.assertEqual(result.headers["cache-control"], "no-store")
            for owner in ("chat:b", "agent:c"):
                self.assertEqual(self.client.get(f"/api/computer/{owner}/last").status_code, 403)
            with patch.object(computer_routes.auth_manager, "is_admin", return_value=True):
                self.assertEqual(self.client.get("/api/computer/agent:c/last").status_code, 200)
        with patch.object(computer_routes.computer.manager, "last", return_value=None):
            self.assertEqual(self.client.get("/api/computer/chat:a/last").status_code, 404)

    def test_history_cannot_read_another_chats_run(self):
        with patch.object(computer_routes.store, "get_run", return_value={"session_id": "b"}):
            self.assertEqual(self.client.get("/api/computer/chat:a/history?run_id=r").status_code, 404)
        with patch.object(computer_routes.store, "get_run", return_value={"session_id": "a"}), \
             patch.object(computer_routes.store, "run_events", return_value=[{"name": "computer", "detail": "frame"}, {"name": "Bash"}]):
            self.assertEqual(self.client.get("/api/computer/chat:a/history?run_id=r").json()["steps"], [{"name": "computer", "detail": "frame"}])

    def test_frames_end_when_computer_closes(self):
        frame = {"owner": "chat:a", "url": "https://example.com/", "title": "Example",
                 "last_action": 1, "taken_over": False, "waiting_model": False, "image": "jpeg"}
        class Connected:
            scope = {"scheme": "http", "server": ("127.0.0.1", 8420)}
            async def is_disconnected(self):
                return False
        async def collect():
            response = await computer_routes.frames("chat:a", Connected(), user="alice")
            return "".join([part async for part in response.body_iterator])
        seen = {}
        async def watch(owner, local=False):
            seen["local"] = local
            yield frame  # one pushed frame, then the computer closes
        with patch.object(computer_routes.computer.manager, "is_running", return_value=True), \
             patch.object(computer_routes.computer.manager, "watch", new=watch), \
             patch.object(computer_routes, "get_current_user", return_value="alice"):
            result = asyncio.run(collect())
        self.assertEqual(result.count('event: frame'), 1)
        self.assertTrue(result.endswith('event: closed\ndata: {}\n\n'))
        self.assertTrue(seen["local"], "plain http on loopback is this computer")
        Connected.scope = {"scheme": "https", "server": ("100.64.1.2", 8443)}
        with patch.object(computer_routes.computer.manager, "is_running", return_value=True), \
             patch.object(computer_routes.computer.manager, "watch", new=watch), \
             patch.object(computer_routes, "get_current_user", return_value="alice"):
            asyncio.run(collect())
        self.assertFalse(seen["local"], "Remote Access (HTTPS on the tailnet) is not local")

class WatchTests(unittest.IsolatedAsyncioTestCase):
    """The live view: pushed screencast frames, never queued behind a command."""

    async def asyncSetUp(self):
        self.procs = []

        async def factory(*args, **kwargs):
            proc = FakeProc()
            self.procs.append(proc)
            return proc

        self.manager = computer.ComputerManager(factory)
        self.patches = [patch("core.computer.sandbox.available", new=AsyncMock(return_value=(True, ""))),
                        patch("core.computer.sandbox_egress.ensure", new=AsyncMock()),
                        patch("core.computer.computer_image.ensure_image", new=AsyncMock(return_value="kairos-computer:fake"))]
        for p in self.patches:
            p.start()
        self.c = await self.manager._get("chat:w")

    async def asyncTearDown(self):
        await self.manager.close_all()
        for p in self.patches:
            p.stop()

    def push(self, data: bytes):
        self.procs[0].stdout.feed_data((json.dumps({"event": "frame", "data": base64.b64encode(data).decode()}) + "\n").encode())

    async def test_frames_are_pushed_and_the_screencast_runs_only_while_watched(self):
        watch = self.manager.watch("chat:w", heartbeat=0.05)
        self.push(b"\xff\xd8one")
        first = await anext(watch)
        self.assertEqual(base64.b64decode(first["image"]), b"\xff\xd8one")
        self.assertEqual([c["action"] for c in self.procs[0].calls], ["watch_start"])
        idle = await anext(watch)  # no new frame: state only, no image re-sent
        self.assertNotIn("image", idle)
        self.push(b"\xff\xd8two")
        self.assertEqual(base64.b64decode((await anext(watch))["image"]), b"\xff\xd8two")
        await watch.aclose()
        self.assertEqual(self.procs[0].calls[-1]["action"], "watch_stop")

    async def test_a_frame_between_replies_does_not_confuse_a_command(self):
        self.push(b"\xff\xd8frame")
        reply = await self.manager._command(self.c, "state")
        self.assertEqual(reply["id"], self.procs[0].calls[-1]["id"])
        self.assertEqual(self.c.frame_jpeg, b"\xff\xd8frame")

    def rates(self):
        return [(c["action"], c.get("fps")) for c in self.procs[0].calls if c["action"].startswith("watch_")]

    async def test_local_control_gets_30_fps_and_watching_gets_10(self):
        watch = self.manager.watch("chat:w", heartbeat=0.05, local=True)
        self.push(b"\xff\xd8one")
        await anext(watch)
        self.assertEqual(self.rates(), [("watch_start", 10)])
        self.manager.takeover("chat:w")
        await asyncio.sleep(0.05)
        self.assertEqual(self.rates()[-1], ("watch_rate", 30))
        self.manager.hand_back("chat:w")
        await asyncio.sleep(0.05)
        self.assertEqual(self.rates()[-1], ("watch_rate", 10))
        await watch.aclose()
        self.assertEqual(self.rates()[-1], ("watch_stop", None))

    async def test_remote_viewer_stays_at_10_fps_even_in_control(self):
        watch = self.manager.watch("chat:w", heartbeat=0.05, local=False)
        self.push(b"\xff\xd8one")
        await anext(watch)
        self.manager.takeover("chat:w")
        await asyncio.sleep(0.05)
        self.assertEqual(self.rates(), [("watch_start", 10)], "no 30 fps for a Remote Access viewer")
        # Frames arriving faster than 10 a second reach this viewer at most every 0.1 s.
        started = asyncio.get_running_loop().time()
        images = 0
        while images < 3:
            self.push(b"\xff\xd8" + bytes([images]))
            if "image" in await anext(watch):
                images += 1
        self.assertGreaterEqual(asyncio.get_running_loop().time() - started, 0.18)
        await watch.aclose()

    async def test_watch_ends_when_the_computer_stops(self):
        watch = self.manager.watch("chat:w", heartbeat=0.05)
        self.push(b"\xff\xd8one")
        await anext(watch)
        await self.manager.stop("chat:w")
        with self.assertRaises(StopAsyncIteration):
            await anext(watch)


class RenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_prompt_sentence_only_for_enabled_admin_desktop(self):
        from core import system_prompt
        for enabled, desktop, admin in ((True, True, True), (True, False, True), (False, True, True), (True, True, False)):
            with patch("core.settings.get_setting", return_value={"enabled": enabled, "desktop": desktop, "allow_non_admins": True}):
                for prompt in (system_prompt.for_claude(admin), system_prompt.for_external(admin), system_prompt.for_codex("python", "cli", admin)):
                    self.assertEqual("files stay in its Documents folder" in prompt, enabled and desktop and admin)
                self.assertNotIn("files stay in its Documents folder", system_prompt.for_external(admin, computer_available=False))

    async def test_computer_guidance_is_gated_on_every_surface(self):
        from core import system_prompt
        instruction = "To open, visit, search or operate a website, use the computer tool."
        for enabled, allow, admin, expected in ((False, False, True, False), (True, False, False, False),
                                                (True, False, True, True), (True, True, False, True)):
            with self.subTest(enabled=enabled, allow=allow, admin=admin), \
                 patch("core.settings.get_setting", return_value={"enabled": enabled, "allow_non_admins": allow}):
                for prompt in (system_prompt.for_claude(admin), system_prompt.for_external(admin),
                               system_prompt.for_codex("python", "cli", admin)):
                    self.assertEqual(instruction in prompt, expected)
                self.assertNotIn(instruction, system_prompt.for_external(admin, computer_available=False))

    async def test_claude_only_computer_is_loaded_upfront_when_allowed(self):
        from core.hive_mind_server import get_hive_mind_server
        from mcp import types
        with patch("core.settings.get_setting", return_value={"enabled": True}):
            config = get_hive_mind_server("s1", True)
        handler = config["instance"]._request_handlers["tools/list"].handler
        listed = await handler(None, types.PaginatedRequestParams())
        eager = [tool.name for tool in listed.tools if (tool.meta or {}).get("anthropic/alwaysLoad")]
        self.assertEqual(eager, ["computer"])

    async def test_model_thumbnail_is_downscaled_capped_and_saved_in_run_event(self):
        from core import runs, session_manager_store as store
        from core.computer_history import THUMBNAIL_LIMIT
        source = Image.frombytes("RGB", (1280, 800), os.urandom(1280 * 800 * 3))
        output = io.BytesIO(); source.save(output, "PNG")
        ctx = tool_registry.ToolContext(session_id="s1", is_admin=True)
        context = runs.RunContext("chat", session_id="s1")
        runs.enter(context)
        try:
            with patch("core.settings.get_setting", return_value={"enabled": True}), \
                 patch("core.computer.manager.act", new=AsyncMock(return_value={
                    "text": "URL: https://example.com/ | open completed.", "images": [output.getvalue()]})):
                await tool_registry.call("computer", {"action": "open", "url": "https://example.com/", "text": "never kept"}, ctx, "claude")
            runs.record(context, runs.Tally(), "finished")
            steps = store.run_events(context.run_id)
            detail = json.loads(steps[-1]["detail"])
            data = base64.b64decode(detail["image"])
            self.assertLessEqual(len(data), THUMBNAIL_LIMIT)
            self.assertEqual(detail["action"], "open")
            with Image.open(io.BytesIO(data)) as thumbnail:
                self.assertEqual(thumbnail.format, "JPEG")
                self.assertLess(thumbnail.width, 1280)
            self.assertNotIn("never kept", str(steps))
            self.assertEqual(context.computer_queue.qsize(), 2)
        finally:
            runs.leave(context)
            runs.CURRENT.set(None)

    async def test_container_driver_source_compiles(self):
        compile(SOURCE, "computer_driver container source", "exec")

    async def test_settings_and_plain_tool(self):
        ctx = tool_registry.ToolContext(session_id="s1", is_admin=True)
        with patch("core.settings.get_setting", return_value={"enabled": False}):
            self.assertIn("computer use is off", await tool_registry.call("computer", {"action": "open"}, ctx, "openai"))
        with patch("core.settings.get_setting", return_value={"enabled": True, "allow_non_admins": False}):
            refused = await tool_registry.call("computer", {"action": "open"},
                tool_registry.ToolContext(session_id="s1"), "openai")
            self.assertIn("limited to admins", refused)
        plain = await tool_registry.call("list_notes", {}, ctx, "openai")
        self.assertIsInstance(plain, str)
        self.assertIn("computer", [s.name for s in tool_registry.specs("codex")])
        self.assertNotIn("computer", [s.name for s in tool_registry.specs("openai", helper=True)])

    async def test_claude_blocks_and_provider_image_messages(self):
        from core.hive_mind_server import _handler
        image = tool_registry.ToolResult("seen", [PNG])
        with patch("core.tool_registry.call", new=AsyncMock(return_value=image)):
            result = await _handler("computer", tool_registry.ToolContext())({})
        self.assertEqual(result["content"][1]["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(result["content"][1]["data"]), PNG)
        text, messages = openai_compatible._tool_messages(image, True, None)
        self.assertEqual(text, "seen")
        self.assertEqual(messages[0]["content"][0]["type"], "image_url")
        _, native = openai_compatible._tool_messages(image, True, "openai")
        self.assertEqual(native_api._responses_input(native)[0]["content"][0]["type"], "input_image")
        _, anthropic = openai_compatible._tool_messages(image, True, "anthropic")
        self.assertEqual(native_api._anthropic_messages(anthropic)[1][0]["content"][0]["type"], "image")
        self.assertIn("use computer read", openai_compatible._tool_messages(image, False, None)[0])

    async def test_codex_saved_file_text_and_cleanup(self):
        app = FastAPI(); app.include_router(tool_routes.router)
        token = tool_access.issue("s1", True)
        with patch("core.tool_registry.dispatch", new=AsyncMock(return_value=tool_registry.ToolResult("seen", [PNG]))):
            response = TestClient(app, client=("127.0.0.1", 50000)).post("/api/tools/computer",
                json={"arguments": {"action": "screenshot"}}, headers={"X-JARVIS-Tool-Token": token})
        path = Path(response.json()["result"].split("Screenshot saved: ")[1].split(". Look at it")[0])
        self.assertEqual(path.parent.parent.resolve(), tool_access.SCREEN_ROOT.resolve())
        self.assertEqual(path.read_bytes(), PNG)
        grant = tool_access.resolve(token)
        outside = environment / "outside" / path.parent.name
        self.assertIsNone(tool_access.safe_screenshot_dir(replace(grant, screenshot_dir=str(outside))))
        tool_access.revoke(token)
        self.assertFalse(path.exists())

    async def test_screenshot_startup_sweep(self):
        old = tool_access.SCREEN_ROOT / "0123456789abcdef"
        fresh = tool_access.SCREEN_ROOT / "fedcba9876543210"
        old.mkdir(parents=True)
        fresh.mkdir(parents=True)
        (old / "image.png").write_bytes(PNG)
        os.utime(old, (0, 0))
        tool_access.sweep_screens()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    async def test_registry_audit_omits_typed_text(self):
        ctx = tool_registry.ToolContext(session_id="s1", is_admin=True)
        answer = {"text": "URL: https://example.com/ | Title: Example | type completed.", "images": [PNG]}
        with patch("core.settings.get_setting", return_value={"enabled": True}), \
             patch("core.computer.manager.act", new=AsyncMock(return_value=answer)), \
             patch("core.permissions.record_tool_use") as audit:
            result = await tool_registry.call("computer", {"action": "type", "text": "secret"}, ctx, "openai")
        self.assertIsInstance(result, tool_registry.ToolResult)
        self.assertEqual(audit.call_args.args[3], {"action": "type", "url": "https://example.com/"})

    async def test_click_audit_uses_current_url_without_typed_text(self):
        ctx = tool_registry.ToolContext(session_id="s1", is_admin=True)
        answer = {"text": computer._opaque_frame_message(), "images": []}
        with patch("core.settings.get_setting", return_value={"enabled": True}), \
             patch("core.computer.manager.act", new=AsyncMock(return_value=answer)), \
             patch("core.computer.manager.running", return_value=[{"owner": "chat:s1", "url": "https://example.com/checkout"}]), \
             patch("core.permissions.record_tool_use") as audit:
            await tool_registry.call("computer", {"action": "click", "x": 4, "y": 5, "text": "secret"}, ctx, "openai")
        self.assertEqual(audit.call_args.args[3], {"action": "click", "url": "https://example.com/checkout"})
        self.assertEqual(audit.call_args.args[0], "tool refused")

    async def test_unavailable_sandbox_has_no_host_fallback(self):
        from core.sandbox import SandboxUnavailable
        ctx = tool_registry.ToolContext(session_id="s1", is_admin=True)
        with patch("core.settings.get_setting", return_value={"enabled": True}), \
             patch("core.computer.manager.act", new=AsyncMock(side_effect=SandboxUnavailable("Docker is off"))):
            answer = await tool_registry.call("computer", {"action": "open", "url": "https://example.com"},
                                              ctx, "openai")
        self.assertIn("sandbox is unavailable", answer)

    async def test_provider_round_adds_image_after_tool_result(self):
        call = {"id": "c1", "function": {"name": "computer", "arguments": '{"action":"screenshot"}'}}
        replies = [{"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}]},
                   {"choices": [{"message": {"role": "assistant", "content": "done"}}]}]
        rounds = []
        with patch("core.providers.openai_compatible._post_chat", new=AsyncMock(side_effect=replies)) as post:
            events = [item async for item in openai_compatible.turn_events("https://local", "m", None,
                [{"role": "user", "content": "look"}], tools=[{"type": "function", "function": {"name": "computer"}}],
                tool_executor=AsyncMock(return_value=tool_registry.ToolResult("seen", [PNG])),
                rounds=rounds, supports_images=True, stream=False)]
        sent = post.call_args_list[1].args[3]["messages"]
        self.assertEqual(sent[-2]["role"], "tool")
        self.assertEqual(sent[-2]["content"], "seen")
        self.assertEqual(sent[-1]["content"][0]["type"], "image_url")
        self.assertEqual(rounds[-1]["role"], "user")


def tearDownModule():
    from core import session_manager_store
    session_manager_store.close()
    assert Path(__file__).resolve().parents[1] in environment.resolve().parents
    shutil.rmtree(environment)


if __name__ == "__main__":
    unittest.main()
