"""Connectors (core/connectors): the shared rules for incoming messages,
the store, delivery, and every platform adapter against a simulated
service. No real account or network is used. Run: python scripts/test_connectors.py
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_DATA = tempfile.TemporaryDirectory(prefix="jarvis-connectors-test-", ignore_cleanup_errors=True)
os.environ["JARVIS_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from core.connectors import KINDS, store  # noqa: E402
from core.connectors.base import Inbound  # noqa: E402
from core.connectors.hub import Hub, chunk  # noqa: E402
from services.agent_service import agent_service  # noqa: E402

OWNER, STRANGER = "owner-1", "stranger-9"


class Stop(Exception):
    """Ends an adapter's receive loop from inside the simulated service."""


class FakeAPI:
    """A simulated platform: answers requests from a list of
    (method, url fragment, reply) rules, the first match winning, and keeps
    every request it saw."""

    def __init__(self, *rules):
        self.rules, self.requests = list(rules), []

    def add(self, *rules):
        self.rules[:0] = list(rules)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, fragment, reply in self.rules:
            if request.method == method and fragment in str(request.url):
                result = reply(request) if callable(reply) else reply
                if isinstance(result, Exception):
                    raise result
                return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)
        return httpx.Response(404, json={"error": "no rule"})

    def sent(self, method, fragment):
        return [r for r in self.requests if r.method == method and fragment in str(r.url)]


def once(first, then):
    """A reply that is `first` the first time and `then` after."""
    calls = {"n": 0}

    def reply(request):
        calls["n"] += 1
        return first if calls["n"] == 1 else then
    return reply


class ConnectorTestCase(unittest.TestCase):
    kind = ""
    values: dict = {}

    def setUp(self):
        self.turn = AsyncMock(return_value="JARVIS reply")
        p = patch("services.chat_service.send_message", self.turn)
        p.start()
        self.addCleanup(p.stop)
        self.api = FakeAPI()
        self.hub = Hub(KINDS, transport=httpx.MockTransport(self.api))
        if self.kind:
            self.record = store.create(self.kind, self.kind, self.values, KINDS[self.kind].fields, [OWNER], False, None)
            self.addCleanup(store.delete, self.record["id"])
            self.adapter = self.hub.build(self.record)

    def run_until_stop(self, coroutine):
        try:
            asyncio.run(coroutine)
        except Stop:
            pass


# -- the shared rules -----------------------------------------------------------------

class RulesTests(ConnectorTestCase):
    kind = "webhook"
    values = {"secret": "s3cret", "reply_url": "https://example.com/replies"}

    def setUp(self):
        super().setUp()
        self.api.add(("POST", "example.com/replies", {}))

    def receive(self, sender, text, replying_to="", record_changes=None):
        if record_changes:
            self.adapter.record.update(record_changes)
        asyncio.run(self.hub.receive(self.adapter, Inbound(conversation="c1", sender=sender, text=text,
                                                           replying_to=replying_to)))

    def test_an_allowed_sender_gets_an_everyday_chat_reply(self):
        from core.session_manager import session_manager
        self.receive(OWNER, "hello")
        session_id, text, _ = self.turn.call_args.args
        self.assertEqual(text, "hello")
        self.assertFalse(self.turn.call_args.kwargs.get("is_admin", False), "connector chats are not admin")
        self.assertIsNone(session_manager.get_session(session_id).get("agent_id"))
        self.assertEqual(json.loads(self.api.sent("POST", "replies")[0].content)["text"], "JARVIS reply")

    def test_tab_hooks_run_only_after_connector_admission(self):
        with patch("core.tab_hooks.emit_message") as captured:
            self.receive(STRANGER, "Private message")
            captured.assert_not_called()
            self.receive(OWNER, "Allowed message")
            captured.assert_called_once()
            self.assertEqual(captured.call_args.args[0], {"kind": "connector", "connection_id": self.record["id"]})
            self.assertEqual(captured.call_args.args[1]["text"], "Allowed message")

    def test_strangers_are_ignored_unless_the_connector_is_open(self):
        self.receive(STRANGER, "hello")
        self.assertFalse(self.turn.called)
        self.receive(STRANGER, "hello", record_changes={"open": True})
        self.assertTrue(self.turn.called)

    def test_only_allowed_senders_on_a_closed_connector_reach_agents(self):
        agent = agent_service.create("Scout")
        self.addCleanup(agent_service.delete, agent["id"])
        with patch.object(agent_service, "chat_endpoint", return_value="claude"):
            self.receive(OWNER, "Scout, status?")
            self.assertEqual((self.turn.call_args.args[1], self.turn.call_args.kwargs.get("is_admin")), ("status?", True))
            self.turn.reset_mock()
            self.receive(OWNER, "Scout, status?", record_changes={"open": True})
            self.assertFalse(self.turn.call_args.kwargs.get("is_admin", False), "an open connector never reaches agents")
            self.assertEqual(self.turn.call_args.args[1], "Scout, status?")

    def test_long_replies_are_split_to_the_platform_limit(self):
        pieces = chunk("a" * 5000 + "\n\n" + "b" * 100, 4096)
        self.assertTrue(all(len(p) <= 4096 for p in pieces))
        self.assertEqual("".join(pieces).replace("\n", ""), "a" * 5000 + "b" * 100)

    def test_secrets_are_stored_encrypted_and_never_shown(self):
        raw = open(store.CONNECTORS_FILE, encoding="utf-8").read()
        self.assertNotIn("s3cret", raw)
        shown = store.public(store.get_record(self.record["id"]), KINDS["webhook"].fields)
        self.assertNotIn("secrets", shown)
        self.assertEqual(shown["secrets_set"], {"secret": True})
        store.update(self.record["id"], KINDS["webhook"].fields, values={"secret": ""})
        self.assertEqual(store.secrets_of(store.get_record(self.record["id"]))["secret"], "s3cret",
                         "a blank secret field keeps the saved one")

    def test_a_failing_connector_reports_why_and_retries(self):
        runs = []

        async def run(adapter_self):
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("token rejected")
            await asyncio.Event().wait()

        async def scenario():
            with patch("core.connectors.hub.RESTART_BACKOFF", (0.2,)), patch.object(KINDS["webhook"], "run", run):
                await self.hub.start(self.record["id"])
                await asyncio.sleep(0.05)
                during = dict(self.hub.status[self.record["id"]])
                await asyncio.sleep(0.3)
                after = dict(self.hub.status[self.record["id"]])
                await self.hub.stop(self.record["id"])
            return during, after
        during, after = asyncio.run(scenario())
        self.assertEqual(during["state"], "error")
        self.assertIn("token rejected", during["detail"])
        self.assertEqual((after["state"], len(runs)), ("connected", 2), "it tried again by itself")

    def test_deliveries_reach_a_connector_through_the_channel_list(self):
        from core.channels import registry
        listed = [c["id"] for c in registry.list_channels()]
        self.assertIn(f"conn:{self.record['id']}", listed)

        async def deliver():
            self.hub.adapters[self.record["id"]] = self.adapter
            with patch("core.connectors.hub", self.hub):
                return await registry.send_to_channel(f"conn:{self.record['id']}", "Daily brief")
        self.assertTrue(asyncio.run(deliver()))
        self.assertEqual(json.loads(self.api.sent("POST", "replies")[-1].content)["text"], "Daily brief")


class RouteTests(ConnectorTestCase):
    def test_managing_connectors_is_admin_only_and_validated(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from core.middleware import require_admin
        from routes import connector_routes
        app = FastAPI()
        app.include_router(connector_routes.router)

        def not_admin():
            raise HTTPException(status_code=403)
        app.dependency_overrides[require_admin] = not_admin
        self.assertEqual(TestClient(app).get("/api/connectors").status_code, 403)
        app.dependency_overrides[require_admin] = lambda: "david"
        client = TestClient(app)
        self.assertEqual(client.post("/api/connectors", json={"kind": "telegram", "values": {}}).status_code, 400,
                         "the bot token is required")
        self.assertEqual(client.post("/api/connectors", json={"kind": "nope"}).status_code, 400)
        self.assertEqual(client.post("/api/connectors/unknown/webhook", content=b"{}").status_code, 404)
        kinds = {k["kind"] for k in client.get("/api/connectors/kinds").json()}
        self.assertEqual(kinds, set(KINDS))


# -- two-way platforms without a public URL ---------------------------------------------

class TelegramTests(ConnectorTestCase):
    kind, values = "telegram", {"bot_token": "T", "default_chat_id": "42"}

    def test_receives_replies_and_downloads_files(self):
        update = {"update_id": 7, "message": {"chat": {"id": 42}, "from": {"id": OWNER, "username": "dd"},
                                                "caption": "look", "document": {"file_id": "F", "file_name": "a.txt"},
                                                "reply_to_message": {"from": {"id": 99}, "text": "earlier note"}}}
        self.api.add(("GET", "/getMe", {"ok": True, "result": {"id": 99}}),
                     ("GET", "/getUpdates", once({"ok": True, "result": [update]}, Stop())),
                     ("GET", "/getFile", {"ok": True, "result": {"file_path": "docs/a.txt"}}),
                     ("GET", "/file/botT/docs/a.txt", httpx.Response(200, content=b"hi")),
                     ("POST", "/sendMessage", {"ok": True}))
        with patch.object(self.hub, "receive", wraps=self.hub.receive) as received:
            self.run_until_stop(self.adapter.run())
        inbound = received.call_args.args[1]
        self.assertEqual((inbound.text, inbound.sender, inbound.replying_to), ("look", OWNER, "earlier note"))
        self.assertEqual(inbound.attachments, [("a.txt", b"hi")])
        self.assertEqual(json.loads(self.api.sent("POST", "/sendMessage")[0].content), {"chat_id": "42", "text": "JARVIS reply"})
        second_poll = self.api.sent("GET", "/getUpdates")[1]
        self.assertEqual(second_poll.url.params["offset"], "8", "never sees the same update twice")


class FakeSocket:
    def __init__(self, frames):
        self.frames, self.sent = list(frames), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, data):
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return json.dumps(self.frames.pop(0))


class SlackTests(ConnectorTestCase):
    kind, values = "slack", {"app_token": "xapp", "bot_token": "xoxb", "default_channel": "C1"}

    def test_acks_events_skips_its_own_and_reads_thread_parents(self):
        self.api.add(("POST", "/auth.test", {"ok": True, "user_id": "UBOT"}),
                     ("POST", "/apps.connections.open", {"ok": True, "url": "wss://slack.test"}),
                     ("GET", "/conversations.replies", {"ok": True, "messages": [{"user": "UBOT", "text": "q [S-abcdef]"}]}),
                     ("GET", "/chat.getPermalink", {"ok": True, "permalink": "https://workspace.slack.com/archives/C1/p2"}),
                     ("POST", "/chat.postMessage", {"ok": True}))
        socket = FakeSocket([
            {"type": "hello"},
            {"type": "events_api", "envelope_id": "e1", "payload": {"event": {"type": "message", "user": "UBOT", "text": "mine", "channel": "C1"}}},
            {"type": "events_api", "envelope_id": "e2", "payload": {"event": {"type": "message", "user": OWNER,
                "text": "<@UBOT> hi", "channel": "C1", "ts": "2", "thread_ts": "1"}}},
        ])
        self.adapter.connect = lambda url: socket
        with patch.object(self.hub, "receive", wraps=self.hub.receive) as received:
            asyncio.run(self.adapter.run())
        self.assertEqual([s["envelope_id"] for s in socket.sent], ["e1", "e2"])
        self.assertEqual(received.call_count, 1, "its own message is skipped")
        inbound = received.call_args.args[1]
        self.assertEqual((inbound.text, inbound.replying_to), ("hi", "q [S-abcdef]"))
        self.assertEqual(inbound.source_url, "https://workspace.slack.com/archives/C1/p2")
        self.assertEqual(inbound.source_context, "q [S-abcdef]")
        self.assertEqual(self.api.sent("GET", "/chat.getPermalink")[0].url.params["message_ts"], "2")
        self.assertEqual(json.loads(self.api.sent("POST", "/chat.postMessage")[0].content)["channel"], "C1")
        self.assertEqual(self.api.sent("POST", "/apps.connections.open")[0].headers["authorization"], "Bearer xapp")


class MattermostTests(ConnectorTestCase):
    kind, values = "mattermost", {"server_url": "https://mm.test", "token": "tok"}

    def test_authenticates_receives_and_posts(self):
        post = {"user_id": OWNER, "channel_id": "ch", "message": "hi", "root_id": "r1"}
        self.api.add(("GET", "/users/me", {"id": "BOT"}), ("GET", "/posts/r1", {"user_id": "BOT", "message": "earlier"}),
                     ("POST", "/api/v4/posts", httpx.Response(201, json={})))
        socket = FakeSocket([{"event": "hello"}, {"event": "posted", "data": {"post": json.dumps({**post, "user_id": "BOT"})}},
                             {"event": "posted", "data": {"post": json.dumps(post)}}])
        self.adapter.connect = lambda url: socket
        with patch.object(self.hub, "receive", wraps=self.hub.receive) as received:
            asyncio.run(self.adapter.run())
        self.assertEqual(socket.sent[0]["action"], "authentication_challenge")
        self.assertEqual(received.call_count, 1)
        self.assertEqual(received.call_args.args[1].replying_to, "earlier")
        self.assertEqual(json.loads(self.api.sent("POST", "/api/v4/posts")[0].content), {"channel_id": "ch", "message": "JARVIS reply"})


class MatrixTests(ConnectorTestCase):
    kind, values = "matrix", {"homeserver": "https://hs.test", "user_id": "@bot:hs", "access_token": "tok"}

    def test_joins_rooms_allowed_users_invite_and_replies(self):
        message = {"type": "m.room.message", "sender": OWNER, "content": {"body": "hi", "m.relates_to": {"m.in_reply_to": {"event_id": "$e"}}}}
        second = {"next_batch": "b2", "rooms": {"invite": {"!r:hs": {"invite_state": {"events": [{"sender": OWNER}]}}},
                                                "join": {"!r:hs": {"timeline": {"events": [message]}}}}}
        sync_calls = {"n": 0}

        def sync(request):
            sync_calls["n"] += 1
            return {"next_batch": "b1"} if sync_calls["n"] == 1 else second if sync_calls["n"] == 2 else Stop()
        self.api.add(("GET", "/sync", sync),
                     ("POST", "/join/", {}), ("GET", "/event/", {"sender": "@bot:hs", "content": {"body": "asked earlier"}}),
                     ("PUT", "/send/m.room.message/", {"event_id": "$x"}))
        with patch.object(self.hub, "receive", wraps=self.hub.receive) as received:
            self.run_until_stop(self.adapter.run())
        self.assertTrue(self.api.sent("POST", "/join/"))
        self.assertEqual(received.call_args.args[1].replying_to, "asked earlier")
        self.assertEqual(json.loads(self.api.sent("PUT", "/send/")[0].content)["body"], "JARVIS reply")


class SignalTests(ConnectorTestCase):
    kind, values = "signal", {"api_url": "http://sig.test", "number": "+1000", "poll_seconds": "2"}

    def test_receives_quotes_and_sends_to_groups(self):
        envelope = {"envelope": {"sourceNumber": OWNER, "dataMessage": {"message": "hi", "groupInfo": {"groupId": "G1"},
                                                                         "quote": {"authorNumber": "+1000", "text": "q [S-abcdef]"}}}}
        self.api.add(("GET", "/v1/receive/", once([envelope], Stop())), ("POST", "/v2/send", httpx.Response(201, json={})))
        with patch.object(self.hub, "receive", wraps=self.hub.receive) as received, \
             patch("asyncio.sleep", AsyncMock()):
            self.run_until_stop(self.adapter.run())
        inbound = received.call_args.args[1]
        self.assertEqual((inbound.conversation, inbound.replying_to), ("group:G1", "q [S-abcdef]"))
        body = json.loads(self.api.sent("POST", "/v2/send")[0].content)
        self.assertEqual(body["recipients"], ["group." + base64.b64encode(b"G1").decode()])


class BlueBubblesTests(ConnectorTestCase):
    kind, values = "bluebubbles", {"server_url": "http://bb.test", "password": "pw"}

    def test_polls_new_messages_and_sends(self):
        message = {"text": "hi", "isFromMe": False, "dateCreated": 10**13, "handle": {"address": OWNER},
                   "chats": [{"guid": "iMessage;-;owner"}], "threadOriginatorGuid": "g0"}
        self.api.add(("POST", "/message/query", once({"data": [message, {**message, "isFromMe": True}]}, Stop())),
                     ("GET", "/message/g0", {"data": {"isFromMe": True, "text": "q [S-abcdef]"}}),
                     ("POST", "/message/text", {"status": 200}))
        with patch.object(self.hub, "receive", wraps=self.hub.receive) as received, patch("asyncio.sleep", AsyncMock()):
            self.run_until_stop(self.adapter.run())
        self.assertEqual(received.call_count, 1, "its own message is skipped")
        self.assertEqual(received.call_args.args[1].replying_to, "q [S-abcdef]")
        sent = self.api.sent("POST", "/message/text")[0]
        self.assertEqual((json.loads(sent.content)["chatGuid"], sent.url.params["password"]), ("iMessage;-;owner", "pw"))


class EmailTests(ConnectorTestCase):
    kind = "email"
    values = {"address": "jarvis@example.com", "password": "pw", "imap_host": "imap.test", "smtp_host": "smtp.test"}

    def message(self, sender, dmarc):
        from email.message import EmailMessage
        m = EmailMessage()
        m["From"], m["Subject"], m["Message-ID"] = sender, "Plan", "<m1@test>"
        if dmarc:
            m["Authentication-Results"] = "mx.test; dkim=pass; spf=pass; dmarc=pass"
        m.set_content("Yes please\n\nOn Mon, JARVIS wrote:\n> Shall I book it? [S-abcdef]")
        return m.as_bytes()

    def test_only_dmarc_verified_mail_counts_and_quotes_become_replying_to(self):
        mails = [self.message(f"David <{OWNER}>", True), self.message(f"Forger <{OWNER}>", False)]

        class FakeIMAP:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, *a): pass
            def select(self, *a): return "OK", [b""]
            def search(self, *a): return "OK", [b"1 2"]
            def fetch(self, number, what): return "OK", [(b"x", mails[int(number) - 1])]
        self.adapter._imap = lambda: FakeIMAP()
        found = self.adapter._fetch_new()
        self.assertEqual([i.sender for i in found], [OWNER, f"unverified:{OWNER}"])
        self.assertEqual(found[0].text, "Yes please")
        self.assertIn("[S-abcdef]", found[0].replying_to)

    def test_replies_thread_on_the_original(self):
        sent = []

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, *a): pass
            def send_message(self, m): sent.append(m)
        with patch("smtplib.SMTP_SSL", FakeSMTP):
            asyncio.run(self.adapter.send("david@example.com\x1fPlan\x1f<m1@test>", "Booked."))
        self.assertEqual((sent[0]["To"], sent[0]["Subject"], sent[0]["In-Reply-To"]), ("david@example.com", "Re: Plan", "<m1@test>"))


class IRCTests(ConnectorTestCase):
    kind = "irc"
    values = {"server": "127.0.0.1", "nick": "jarvis", "channels": "#home", "tls": "false"}

    def test_joins_answers_only_when_addressed_and_pongs(self):
        lines_from_bot = []

        async def scenario():
            async def serve(reader, writer):
                writer.write(b":srv 001 jarvis :Welcome\r\nPING :x1\r\n")
                writer.write(f":{OWNER}!u@h PRIVMSG #home :just chatting\r\n".encode())
                writer.write(f":{OWNER}!u@h PRIVMSG #home :jarvis: hi there\r\n".encode())
                await writer.drain()
                for _ in range(40):
                    line = await asyncio.wait_for(reader.readline(), 5)
                    lines_from_bot.append(line.decode().strip())
                    if line.startswith(b"PRIVMSG #home :JARVIS reply"):
                        break
                writer.close()
            server = await asyncio.start_server(serve, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            self.adapter.record["settings"]["port"] = str(port)
            async with server:
                try:
                    await asyncio.wait_for(self.adapter.run(), 10)
                except Exception:
                    pass
        asyncio.run(scenario())
        self.assertIn("JOIN #home", lines_from_bot)
        self.assertIn("PONG :x1", lines_from_bot)
        self.assertIn("PRIVMSG #home :JARVIS reply", lines_from_bot)
        self.assertEqual(self.turn.call_count, 1, "a channel message not addressed to the bot is ignored")
        self.assertEqual(self.turn.call_args.args[1], "hi there")


# -- webhook platforms ---------------------------------------------------------------------

class WebhookPlatformTests(ConnectorTestCase):
    def make(self, kind, values):
        record = store.create(kind, kind, values, KINDS[kind].fields, [OWNER], False, None)
        self.addCleanup(store.delete, record["id"])
        return self.hub.build(record)

    def hook(self, adapter, method, headers, body, query=None):
        async def go():
            result = await adapter.handle_webhook(method, headers, query or {}, body)
            await asyncio.gather(*self.hub._background)
            return result
        return asyncio.run(go())

    def test_twilio_sms_verifies_twilios_signature(self):
        adapter = self.make("sms", {"account_sid": "AC1", "auth_token": "tok", "from_number": "+1999",
                                    "public_url": "https://j.test/api/connectors/x/webhook"})
        self.api.add(("POST", "api.twilio.com", httpx.Response(201, json={})))
        params = {"From": OWNER, "Body": "hi", "To": "+1999"}
        payload = "https://j.test/api/connectors/x/webhook" + "".join(k + v for k, v in sorted(params.items()))
        good = base64.b64encode(hmac.new(b"tok", payload.encode(), hashlib.sha1).digest()).decode()
        body = "&".join(f"{k}={v}" for k, v in params.items()).encode().replace(b"+", b"%2B")
        self.assertEqual(self.hook(adapter, "POST", {"X-Twilio-Signature": "forged"}, body)[0], 403)
        self.assertFalse(self.turn.called)
        self.assertEqual(self.hook(adapter, "POST", {"X-Twilio-Signature": good}, body)[0], 200)
        sms = self.api.sent("POST", "api.twilio.com")[0]
        self.assertIn(b"Body=JARVIS+reply", sms.content)

    def test_whatsapp_verifies_setup_signature_and_reply_context(self):
        adapter = self.make("whatsapp", {"phone_number_id": "P1", "access_token": "at", "app_secret": "sec", "verify_token": "vt"})
        self.assertEqual(self.hook(adapter, "GET", {}, b"", {"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "c"}),
                         (200, b"c", "text/plain"))
        self.assertEqual(self.hook(adapter, "GET", {}, b"", {"hub.mode": "subscribe", "hub.verify_token": "no"})[0], 403)
        self.api.add(("POST", "graph.facebook.com", {"messages": [{"id": "wamid.1"}]}))
        asyncio.run(adapter.send(OWNER, "Question [S-abcdef]"))
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"from": OWNER, "type": "text", "text": {"body": "yes"}, "context": {"id": "wamid.1"}}]}}]}]}).encode()
        sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
        self.assertEqual(self.hook(adapter, "POST", {"X-Hub-Signature-256": "sha256=bad"}, body)[0], 403)
        with patch.object(self.hub, "receive", wraps=self.hub.receive) as received:
            self.assertEqual(self.hook(adapter, "POST", {"X-Hub-Signature-256": sig}, body)[0], 200)
        self.assertEqual(received.call_args.args[1].replying_to, "Question [S-abcdef]")

    def test_line_verifies_signature_and_pushes(self):
        adapter = self.make("line", {"channel_secret": "sec", "access_token": "at"})
        self.api.add(("POST", "api.line.me", {"sentMessages": [{"id": "1"}]}))
        body = json.dumps({"events": [{"type": "message", "message": {"type": "text", "text": "hi"},
                                       "source": {"userId": OWNER}}]}).encode()
        sig = base64.b64encode(hmac.new(b"sec", body, hashlib.sha256).digest()).decode()
        self.assertEqual(self.hook(adapter, "POST", {"X-Line-Signature": "bad"}, body)[0], 403)
        self.assertEqual(self.hook(adapter, "POST", {"X-Line-Signature": sig}, body)[0], 200)
        self.assertEqual(json.loads(self.api.sent("POST", "api.line.me")[0].content)["to"], OWNER)

    def test_generic_webhook_signs_both_ways(self):
        adapter = self.make("webhook", {"secret": "sec", "reply_url": "https://svc.test/in"})
        self.api.add(("POST", "svc.test", {}))
        body = json.dumps({"text": "hi", "sender": OWNER}).encode()
        sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
        self.assertEqual(self.hook(adapter, "POST", {}, body)[0], 403)
        self.assertEqual(self.hook(adapter, "POST", {"X-JARVIS-Signature": sig}, body)[0], 202)
        reply = self.api.sent("POST", "svc.test")[0]
        expected = "sha256=" + hmac.new(b"sec", reply.content, hashlib.sha256).hexdigest()
        self.assertEqual(reply.headers["X-JARVIS-Signature"], expected)


# -- send-only ----------------------------------------------------------------------------------

class SendOnlyTests(ConnectorTestCase):
    def make(self, kind, values):
        record = store.create(kind, kind, values, KINDS[kind].fields, [], False, None)
        self.addCleanup(store.delete, record["id"])
        return self.hub.build(record)

    def test_each_posts_in_its_platforms_format(self):
        cases = [
            ("ntfy", {"topic": "jarvis-x9"}, "ntfy.sh/jarvis-x9", lambda r: self.assertEqual(r.content, b"hello")),
            ("teams", {"webhook_url": "https://teams.test/hook"}, "teams.test",
             lambda r: self.assertEqual(json.loads(r.content)["attachments"][0]["content"]["body"][0]["text"], "hello")),
            ("google_chat", {"webhook_url": "https://chat.test/hook"}, "chat.test",
             lambda r: self.assertEqual(json.loads(r.content), {"text": "hello"})),
            ("feishu", {"webhook_url": "https://feishu.test/hook", "signing_secret": "s"}, "feishu.test",
             lambda r: self.assertIn("sign", json.loads(r.content))),
            ("dingtalk", {"webhook_url": "https://ding.test/robot?access_token=a", "signing_secret": "s"}, "ding.test",
             lambda r: self.assertIn("sign", r.url.params)),
            ("wecom", {"webhook_url": "https://wecom.test/hook"}, "wecom.test",
             lambda r: self.assertEqual(json.loads(r.content)["text"]["content"], "hello")),
        ]
        self.api.add(*[("POST", fragment, {"code": 0, "errcode": 0}) for _, _, fragment, _ in cases])
        for kind, values, fragment, check in cases:
            with self.subTest(kind=kind):
                adapter = self.make(kind, values)
                self.assertEqual(adapter.default_target, "default", "deliveries need no destination field")
                asyncio.run(adapter.send("default", "hello"))
                check(self.api.sent("POST", fragment)[-1])

    def test_a_platform_error_in_the_body_is_reported(self):
        from core.connectors.base import ConnectorError
        adapter = self.make("wecom", {"webhook_url": "https://wecom.test/hook"})
        self.api.add(("POST", "wecom.test", {"errcode": 93000, "errmsg": "invalid webhook url"}))
        with self.assertRaises(ConnectorError):
            asyncio.run(adapter.send("default", "hello"))


if __name__ == "__main__":
    unittest.main()
