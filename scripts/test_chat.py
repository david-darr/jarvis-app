"""Isolated backend regression suite. Never imports the running app or data.
Run: .venv/Scripts/python.exe scripts/test_chat.py
"""
import os
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, AsyncMock
import asyncio

fixture = tempfile.TemporaryDirectory(prefix="jarvis-chat-test-")
os.environ["JARVIS_DATA_DIR"] = fixture.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from core import chat_artifacts, image_gen, middleware
from core.session_manager import session_manager, SessionManager
from core.codex_brain import CodexBrain
from core.brain import Brain
from claude_agent_sdk import AssistantMessage, TextBlock, ResultMessage
from claude_agent_sdk.types import StreamEvent
from routes import chat_routes, session_routes
from services import chat_service

app = FastAPI()
app.add_middleware(middleware.SecurityHeadersMiddleware)
app.include_router(chat_routes.router)
app.include_router(session_routes.router)
client = TestClient(app)
ENDPOINTS = {
    "claude": {"id": "claude", "kind": "claude_cli", "model": "endpoint-model"},
    "codex": {"id": "codex", "kind": "codex_cli", "model": "endpoint-model"},
    "local": {"id": "local", "kind": "local", "model": "local-model"},
}


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.session = session_manager.create_session()
        self.sid = self.session["id"]
        self.root = Path(fixture.name) / self.sid
        self.root.mkdir()
        session_manager.set_workspace(self.sid, str(self.root))
        self.endpoint_patch = patch("core.model_endpoints.get_endpoint", side_effect=lambda key: ENDPOINTS.get(key))
        self.endpoint_patch.start()
        chat_service._busy.clear()
        chat_service._brains.clear()

    def tearDown(self):
        self.endpoint_patch.stop()

    def select(self, endpoint="claude", override=None):
        return client.post(f"/api/sessions/{self.sid}/model", json={"model_endpoint_id": endpoint, "model_override": override})

    def test_variant_persistence_and_no_global_mutation(self):
        self.assertEqual(self.select(override="exact-model-1").status_code, 200)
        self.assertEqual(SessionManager().get_session(self.sid)["model_override"], "exact-model-1")
        self.assertEqual(ENDPOINTS["claude"]["model"], "endpoint-model")
        self.assertIsNone(session_manager.create_session().get("model_override"))
        self.assertEqual(self.select(override="").json()["model_override"], "")
        self.assertIsNone(self.select(override=None).json()["model_override"])

    def test_cli_only_and_validation(self):
        for endpoint, override in [("local", "x"), ("local", ""), (None, "x"), ("missing", None), ("claude", "-m injected"), ("claude", "x" * 161)]:
            self.assertEqual(self.select(endpoint, override).status_code, 400)
        self.assertEqual(self.select("local").status_code, 200)
        self.assertEqual(client.post('/api/sessions/missing/model', json={}).status_code, 404)

    def test_busy_model_and_turn_are_rejected(self):
        chat_service._busy.add(self.sid)
        self.assertEqual(self.select().status_code, 409)
        self.assertEqual(client.post('/api/chat/stream', json={"session_id": self.sid, "message": "Hi"}).status_code, 409)
        self.assertEqual(session_manager.get_session(self.sid)["messages"], [])
        self.assertEqual(client.delete(f'/api/sessions/{self.sid}').status_code, 409)
        self.assertEqual(client.post(f'/api/sessions/{self.sid}/project', json={}).status_code, 409)

    def test_claude_text_deltas_are_not_duplicated(self):
        async def run():
            event = lambda payload: StreamEvent(uuid='test', session_id='sdk-session', event=payload)
            events = [event({'type': 'message_start'}),
                      event({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'not visible'}}),
                      event({'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'Hello '}}),
                      event({'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'world'}}),
                      AssistantMessage(content=[TextBlock(text='Hello world')], model='test'),
                      event({'type': 'message_start'}),
                      AssistantMessage(content=[TextBlock(text='Fallback block')], model='test'),
                      ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id='sdk-session')]
            class FakeSDK:
                query = AsyncMock()
                async def receive_response(self):
                    for item in events: yield item
            brain = Brain(vault_dir=str(self.root)); brain._client = FakeSDK()
            chunks = [chunk async for chunk in brain.run_turn_stream('test')]
            self.assertEqual(chunks, ['Hello ', 'world', '\n\n', 'Fallback block'])
        asyncio.run(run())

    def test_codex_reports_failure_not_success_text(self):
        async def run():
            class FakeProc:
                returncode = 0
                def __init__(self, data):
                    self.stdout = asyncio.StreamReader(); self.stdout.feed_data(data); self.stdout.feed_eof()
                async def wait(self): return 0
            brain = CodexBrain(vault_dir=str(self.root))
            stderr = asyncio.create_task(asyncio.sleep(0, result=b''))
            with self.assertRaises(RuntimeError):
                _ = [chunk async for chunk in brain._consume_process(FakeProc(b'{"type":"turn.failed","error":{"message":"bad model"}}\n'), stderr, True, 'test')]
        asyncio.run(run())

    def test_cli_dispatch_and_thread_preservation(self):
        async def run():
            for kind, cls in [("claude", "Brain"), ("codex", "CodexBrain")]:
                session_manager.set_model_endpoint(self.sid, kind, "exact-model-2")
                with patch.object(chat_service, cls) as factory:
                    factory.return_value.connect = AsyncMock()
                    factory.return_value.disconnect = AsyncMock()
                    await chat_service._get_brain(self.sid, ENDPOINTS[kind])
                    self.assertEqual(factory.call_args.kwargs["model"], "exact-model-2")
                    await chat_service.close_session_brain(self.sid)
            session_manager.set_codex_thread_id(self.sid, "thread-1")
            session_manager.set_model_endpoint(self.sid, "codex", "exact-model-3")
            brain = CodexBrain(session_id=self.sid, model="exact-model-3")
            args = brain._build_args("codex")
            self.assertIn("resume", args)
            self.assertEqual(args[args.index('-m') + 1], "exact-model-3")
            session_manager.append_message(self.sid, "user", "Keep this history")
            session_manager.set_model_endpoint(self.sid, "claude", "exact-model-4")
            self.assertIsNone(session_manager.get_session(self.sid)["codex_thread_id"])
            self.assertEqual(session_manager.get_session(self.sid)["messages"][0]["content"], "Keep this history")
        asyncio.run(run())

    def test_stream_failure_persists_partial(self):
        class FailingBrain:
            async def run_turn_stream(self, text):
                yield "Partial **answer**"
                raise RuntimeError("private provider error")
        self.select()
        with patch.object(chat_service, '_get_brain', AsyncMock(return_value=(FailingBrain(), False))), patch.object(chat_service, 'close_session_brain', AsyncMock()):
            res = client.post('/api/chat/stream', json={"session_id": self.sid, "message": "Hi"})
        self.assertIn('"error":', res.text)
        self.assertNotIn('"done": true', res.text)
        self.assertNotIn('private provider error', res.text)
        last = session_manager.get_session(self.sid)["messages"][-1]
        self.assertEqual(last["content"], "Partial **answer**")
        self.assertEqual(last["status"], "interrupted")
        self.assertFalse(chat_service.is_busy(self.sid))

    def publish(self, name, content=b"hello"):
        path = self.root / name
        path.write_bytes(content)
        res = client.post('/api/chat/artifacts', json={"session_id": self.sid, "path": str(path)})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["url"]

    def test_file_delivery_and_previews(self):
        for name, kind in [('My report.md', 'markdown'), ('index.html', 'html'), ('script.py', 'text'), ('file.pdf', 'pdf'), ('picture.png', 'image'), ('report.docx', 'download')]:
            url = self.publish(name)
            params = {"session_id": self.sid, "url": url}
            res = client.get('/api/chat/artifacts', params=params)
            self.assertEqual(res.json()["kind"], kind)
            self.assertEqual(res.json()["filename"], name)
            self.assertIn(url, SessionManager().get_session(self.sid)["artifact_urls"])
            download = client.get('/api/chat/artifacts/content', params={**params, "download": True})
            self.assertEqual(download.content, b'hello')
            self.assertIn('attachment', download.headers['content-disposition'])
            preview = client.get('/api/chat/artifacts/content', params=params)
            self.assertIn("default-src 'none'", preview.headers['content-security-policy'])
            self.assertEqual(preview.headers['x-content-type-options'], 'nosniff')
            if kind == 'html': self.assertTrue(preview.headers['content-type'].startswith('text/plain'))

    def test_existing_session_link_and_missing_file(self):
        path = self.root / 'legacy.txt'; path.write_text('legacy')
        url = image_gen.register_generated_file(str(path))["url"]
        session_manager.append_message(self.sid, 'assistant', f'[Legacy]({url})')
        self.assertEqual(chat_artifacts.resolve(self.sid, url)[1]['kind'], 'text')
        self.assertEqual(client.get('/api/chat/artifacts', params={"session_id": self.sid, "url": url + 'missing'}).status_code, 404)

    def test_artifact_security_and_size_limits(self):
        url = self.publish('safe.txt')
        other = session_manager.create_session()['id']
        self.assertEqual(client.get('/api/chat/artifacts', params={"session_id": other, "url": url}).status_code, 404)
        for source in ['../outside.txt', '.env', 'C:\\Windows\\win.ini']:
            self.assertEqual(client.post('/api/chat/artifacts', json={"session_id": self.sid, "path": source}).status_code, 400)
        for invalid in ['/generated-files/%2e%2e%2fsecret', '/generated-files/C%3Asecret', 'https://example.test/secret']:
            session_manager.append_message(self.sid, 'assistant', f'[bad]({invalid})')
            self.assertIn(client.get('/api/chat/artifacts', params={"session_id": self.sid, "url": invalid}).status_code, [400, 404])
        url = self.publish('large.txt', b'x' * (chat_artifacts.MAX_PREVIEW_BYTES + 1))
        self.assertEqual(chat_artifacts.resolve(self.sid, url)[1]['kind'], 'download')
        with patch('core.middleware.auth_enabled', return_value=True):
            self.assertEqual(client.get('/api/chat/artifacts', params={"session_id": self.sid, "url": url}).status_code, 401)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        fixture.cleanup()
