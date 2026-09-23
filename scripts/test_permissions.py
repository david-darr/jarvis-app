"""The approval broker, on its own and through its routes.

What matters here is not that allowing works - it is that everything which
should refuse does. Silence, a closed window, a surface nobody is watching, a
stale request id and a grant asked to stretch past its scope all have to end
in a denial, or the feature is worse than not having asked at all.
"""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
environment = tempfile.TemporaryDirectory(prefix="jarvis-permissions-")
os.environ["JARVIS_DATA_DIR"] = environment.name

import httpx
from fastapi import FastAPI
from unittest.mock import patch

from core import middleware, permissions
from routes import permission_routes


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=environment.name)
        self.patch = patch.object(permissions, "PERMISSIONS_FILE",
                                  os.path.join(self.directory.name, "permissions.json"))
        self.patch.start()
        permissions._session_rules.clear()
        permissions._channels.clear()
        permissions._pending.clear()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    async def answer_next(self, queue, choice):
        """Act as the window: take the request, answer it as a person would."""
        request = await asyncio.wait_for(queue.get(), timeout=5)
        self.assertTrue(permissions.answer(request["id"], choice, "alice"))
        return request

    async def test_a_surface_nobody_is_watching_denies_and_says_where_to_grant(self):
        decision = await permissions.decide(surface="chat:none", tool="Bash",
                                            arguments={"command": "ls"}, timeout=1)
        self.assertEqual(decision.behavior, "deny")
        self.assertIn("Settings", decision.reason)

    async def test_silence_denies_and_does_not_pretend_the_owner_refused(self):
        permissions.open_channel("chat:1")
        decision = await permissions.decide(surface="chat:1", tool="Bash",
                                            arguments={"command": "ls"}, timeout=0.2)
        self.assertEqual(decision.behavior, "deny")
        self.assertIn("Nobody answered", decision.reason)

    async def test_closing_the_window_denies_what_was_waiting(self):
        queue = permissions.open_channel("chat:1")
        asking = asyncio.ensure_future(permissions.decide(
            surface="chat:1", tool="Bash", arguments={"command": "ls"}, timeout=30))
        await asyncio.wait_for(queue.get(), timeout=5)
        permissions.close_channel("chat:1")
        decision = await asyncio.wait_for(asking, timeout=5)
        self.assertEqual(decision.behavior, "deny")
        self.assertIn("closed", decision.reason)

    async def test_allow_once_does_not_grant_anything_for_next_time(self):
        queue = permissions.open_channel("chat:1")
        asking = asyncio.ensure_future(permissions.decide(
            surface="chat:1", tool="Bash", arguments={"command": "npm test"}, timeout=30))
        await self.answer_next(queue, "once")
        self.assertEqual((await asking).behavior, "allow")
        self.assertEqual(permissions.list_rules(), [], "once means once")
        self.assertIsNone(permissions.stored_decision("chat:1", "Bash", "npm test"))

    async def test_always_is_scoped_to_what_was_asked_not_to_the_tool(self):
        queue = permissions.open_channel("chat:1")
        asking = asyncio.ensure_future(permissions.decide(
            surface="chat:1", tool="Bash", arguments={"command": "npm test -- --watch"}, timeout=30))
        await self.answer_next(queue, "forever")
        self.assertEqual((await asking).behavior, "allow")
        # The same command family needs no one.
        again = await permissions.decide(surface="chat:2", tool="Bash",
                                         arguments={"command": "npm test --silent"}, timeout=1)
        self.assertEqual(again.behavior, "allow")
        # A different command is a different question, and nobody is watching
        # chat:2, so it denies rather than inheriting the grant.
        other = await permissions.decide(surface="chat:2", tool="Bash",
                                         arguments={"command": "rm -rf"}, timeout=1)
        self.assertEqual(other.behavior, "deny", "granting one command must never grant the shell")

    async def test_a_chat_grant_stays_in_that_chat_and_is_not_written_down(self):
        queue = permissions.open_channel("chat:1")
        asking = asyncio.ensure_future(permissions.decide(
            surface="chat:1", tool="Bash", arguments={"command": "git status"}, timeout=30))
        await self.answer_next(queue, "session")
        self.assertEqual((await asking).behavior, "allow")
        self.assertIsNotNone(permissions.stored_decision("chat:1", "Bash", "git status"))
        self.assertIsNone(permissions.stored_decision("chat:9", "Bash", "git status"))
        self.assertEqual(permissions._load()["rules"], [], "a chat-only grant is never persisted")
        permissions.drop_session("chat:1")
        self.assertIsNone(permissions.stored_decision("chat:1", "Bash", "git status"))

    async def test_rejecting_refuses_and_tells_the_model_why(self):
        queue = permissions.open_channel("chat:1")
        asking = asyncio.ensure_future(permissions.decide(
            surface="chat:1", tool="Bash", arguments={"command": "curl evil"}, timeout=30))
        await self.answer_next(queue, "reject")
        decision = await asking
        self.assertEqual(decision.behavior, "deny")
        self.assertIn("refused by the owner", decision.reason)

    async def test_a_revoked_grant_is_asked_for_again(self):
        queue = permissions.open_channel("chat:1")
        asking = asyncio.ensure_future(permissions.decide(
            surface="chat:1", tool="Bash", arguments={"command": "npm test"}, timeout=30))
        await self.answer_next(queue, "forever")
        await asking
        rule = permissions.list_rules()[0]
        self.assertTrue(permissions.revoke(rule["id"]))
        self.assertIsNone(permissions.stored_decision("chat:1", "Bash", "npm test"))

    async def test_a_stale_or_invented_request_id_answers_nothing(self):
        self.assertFalse(permissions.answer("not-a-real-id", "once", "alice"))
        queue = permissions.open_channel("chat:1")
        asking = asyncio.ensure_future(permissions.decide(
            surface="chat:1", tool="Bash", arguments={"command": "ls"}, timeout=30))
        request = await self.answer_next(queue, "once")
        await asking
        self.assertFalse(permissions.answer(request["id"], "once", "alice"),
                         "an answered request cannot be answered again")

    async def test_every_decision_is_written_down(self):
        queue = permissions.open_channel("chat:1")
        asking = asyncio.ensure_future(permissions.decide(
            surface="chat:1", tool="Bash", arguments={"command": "ls -la"}, timeout=30))
        await self.answer_next(queue, "forever")
        await asking
        entries = permissions.audit()
        self.assertTrue(any(entry.get("decision") == "granted" for entry in entries))
        self.assertTrue(any(entry.get("by") == "alice" for entry in entries))

    async def test_admin_shell_defaults_are_visible_revocable_and_not_reseeded(self):
        permissions.ensure_seeded(["mcp__hive_mind__list_notes", "Bash", "PowerShell", "run_shell"])
        tools = {rule["tool"] for rule in permissions.list_rules()}
        self.assertIn("mcp__hive_mind__list_notes", tools)
        self.assertIn("Bash", tools)
        self.assertIn("PowerShell", tools)
        self.assertNotIn("run_shell", tools)
        self.assertIsNone(permissions.stored_decision("chat:user", "Bash", "git status"),
                          "the built-in shell grant cannot authorize a non-admin caller")
        self.assertEqual(permissions.stored_decision("chat:admin", "Bash", "git status",
                                                     is_admin=True).behavior, "allow")
        shell = next(rule for rule in permissions.list_rules() if rule["tool"] == "Bash")
        self.assertTrue(permissions.revoke(shell["id"]))
        permissions.ensure_seeded(["mcp__hive_mind__list_notes", "Bash", "PowerShell"])
        self.assertNotIn("Bash", permissions.standing_grants(), "revocation must survive a reconnect")
        self.assertEqual(len([r for r in permissions.list_rules() if r["tool"].startswith("mcp__")]), 1,
                         "seeding twice must not duplicate a grant")
        permissions.grant_standing(["Bash"], granted_by="alice")
        self.assertIn("Bash", permissions.standing_grants(), "an admin can explicitly restore it")

    async def test_a_request_shows_the_arguments_as_text(self):
        queue = permissions.open_channel("chat:1")
        asking = asyncio.ensure_future(permissions.decide(
            surface="chat:1", tool="Bash", arguments={"command": "<img src=x onerror=alert(1)>"}, timeout=30))
        request = await asyncio.wait_for(queue.get(), timeout=5)
        self.assertIsInstance(request["arguments"], str)
        self.assertIn("onerror", request["arguments"], "shown verbatim, as text, for the person to judge")
        permissions.answer(request["id"], "reject", "alice")
        await asking


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=environment.name)
        self.patch = patch.object(permissions, "PERMISSIONS_FILE",
                                  os.path.join(self.directory.name, "permissions.json"))
        self.patch.start()
        permissions._session_rules.clear(); permissions._channels.clear(); permissions._pending.clear()
        self.app = FastAPI()
        self.app.include_router(permission_routes.router)
        self.auth = patch("core.middleware.auth_enabled", return_value=True)
        self.auth.start()
        self.tokens = patch.object(middleware.auth_manager, "validate_session",
                                   side_effect=lambda token: {"a": "alice", "b": "bob"}.get(token))
        self.tokens.start()
        self.admin = patch.object(middleware.auth_manager, "is_admin",
                                  side_effect=lambda user: user == "alice")
        self.admin.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in (self.admin, self.tokens, self.auth, self.patch):
            item.stop()
        self.directory.cleanup()

    def headers(self, user):
        return {"Cookie": f"{middleware.SESSION_COOKIE_NAME}={user}"} if user else {}

    async def test_answering_needs_a_user_and_revoking_needs_an_admin(self):
        self.assertEqual((await self.client.get("/api/permissions")).status_code, 401)
        queue = permissions.open_channel("chat:1")
        asking = asyncio.ensure_future(permissions.decide(
            surface="chat:1", tool="Bash", arguments={"command": "npm test"}, timeout=30))
        request = await asyncio.wait_for(queue.get(), timeout=5)

        # A non-admin may answer their own prompt.
        answered = await self.client.post(f"/api/permissions/{request['id']}/answer",
                                          json={"choice": "forever"}, headers=self.headers("b"))
        self.assertEqual(answered.status_code, 200, answered.text)
        self.assertEqual((await asking).behavior, "allow")

        listed = await self.client.get("/api/permissions", headers=self.headers("b"))
        rule = listed.json()["rules"][0]
        # Revoking a global rule is an admin action.
        self.assertEqual((await self.client.delete(f"/api/permissions/{rule['id']}",
                                                   headers=self.headers("b"))).status_code, 403)
        self.assertEqual((await self.client.delete(f"/api/permissions/{rule['id']}",
                                                   headers=self.headers("a"))).status_code, 200)
        self.assertEqual((await self.client.delete(f"/api/permissions/{rule['id']}",
                                                   headers=self.headers("a"))).status_code, 404)

    async def test_answering_something_nobody_is_waiting_for_conflicts(self):
        response = await self.client.post("/api/permissions/made-up/answer",
                                          json={"choice": "once"}, headers=self.headers("a"))
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)
