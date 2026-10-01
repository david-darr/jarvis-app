"""Provider discovery and native chat protocol contracts, without paid calls.

Run: .venv/Scripts/python.exe scripts/test_model_discovery.py
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

fixture = tempfile.TemporaryDirectory(prefix="jarvis-model-catalog-test-")
os.environ["JARVIS_DATA_DIR"] = fixture.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import model_catalog, model_discovery
from core.providers import native_api, openai_compatible


_real_async_client = httpx.AsyncClient


def client_for(handler):
    return _real_async_client(transport=httpx.MockTransport(handler))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        model_discovery._cache.clear()
        model_catalog._codex_live = None

    def test_codex_live_efforts_are_accepted_for_new_models(self):
        live = [{"id": "gpt-new-codex", "display_name": "New Codex", "supported_efforts": [
            {"effort": "ultra", "description": ""}], "context_window": None,
            "effective_context_percent": None}]
        async def run():
            with patch.object(model_discovery, "_codex_cli_models", AsyncMock(return_value=live)), \
                 patch.object(model_catalog, "_load_codex_models", return_value=[]):
                result = await model_discovery.list_for_endpoint({"id": "codex", "kind": "codex_cli"})
            self.assertEqual(result[0]["id"], "gpt-new-codex")
            self.assertTrue(model_catalog.validate_effort("codex_cli", "gpt-new-codex", "ultra"))
        asyncio.run(run())

    def test_claude_cli_uses_current_token_and_paginates_without_returning_it(self):
        seen = []

        def handler(request):
            seen.append(request)
            self.assertEqual(request.headers["authorization"], "Bearer borrowed-token")
            cursor = request.url.params.get("after_id")
            return httpx.Response(200, json={"data": [{"id": "claude-opus-5-5" if cursor else "claude-opus-5",
                                                       "display_name": "Opus 5.5" if cursor else "Opus 5"}],
                                               "has_more": not cursor, "last_id": "first"})

        async def run():
            with tempfile.TemporaryDirectory() as config:
                Path(config, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
                    "accessToken": "borrowed-token", "expiresAt": 9999999999999}}), encoding="utf-8")
                with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": config}), \
                     patch.object(model_discovery.httpx, "AsyncClient", side_effect=lambda **kw: client_for(handler)):
                    first = await model_discovery.list_for_endpoint({"id": "cli-1", "kind": "claude_cli"})
                    second = await model_discovery.list_for_endpoint({"id": "cli-1", "kind": "claude_cli"})
                self.assertIn("claude-opus-5-5", [m["id"] for m in first])
                self.assertEqual(first, second)
                self.assertEqual(len(seen), 2)  # pagination, then cache hit
                self.assertNotIn("borrowed-token", json.dumps(first))
        asyncio.run(run())

    def test_openai_api_catalog_is_scoped_to_credential_and_keeps_stale_on_failure(self):
        calls = []

        def handler(request):
            calls.append(request.headers["authorization"])
            if len(calls) == 3:
                return httpx.Response(503)
            model = "gpt-6-astra" if "key-a" in calls[-1] else "gpt-5.5"
            return httpx.Response(200, json={"data": [{"id": model}, {"id": "text-embedding-3-large"},
                                                     {"id": "next-family-chat"}]})

        async def run():
            ep = {"id": "shared", "kind": "api", "base_url": "https://api.openai.com/v1",
                  "api_key_encrypted": "unused"}
            with patch.object(model_discovery, "_api_key", side_effect=["key-a", "key-b", "key-a"]), \
                 patch.object(model_discovery.httpx, "AsyncClient", side_effect=lambda **kw: client_for(handler)):
                a = await model_discovery.list_for_endpoint(ep)
                b = await model_discovery.list_for_endpoint(ep)
                stale = await model_discovery.list_for_endpoint(ep, force=True)
            self.assertEqual([m["id"] for m in a], ["next-family-chat", "gpt-6-astra"])
            self.assertEqual([m["id"] for m in b], ["next-family-chat", "gpt-5.5"])
            self.assertEqual(stale, a)
        asyncio.run(run())


class NativeTransportTests(unittest.TestCase):
    def test_anthropic_tool_round_trip(self):
        requests = []

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "t1",
                                                              "name": "lookup", "input": {"query": "x"}}],
                                                 "usage": {"input_tokens": 10, "output_tokens": 2}})
            self.assertEqual(body["messages"][-1]["content"][0]["tool_use_id"], "t1")
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Done"}],
                                             "usage": {"input_tokens": 12, "output_tokens": 3}})

        async def run():
            with patch.object(openai_compatible.httpx, "AsyncClient", side_effect=lambda **kw: client_for(handler)):
                answer = await openai_compatible.run_turn(
                    "https://api.anthropic.com", "claude-opus-5-5", "test-key",
                    [{"role": "system", "content": "You help"}, {"role": "user", "content": "Hi"}],
                    tools=[{"type": "function", "function": {"name": "lookup", "description": "Find",
                             "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}],
                    tool_executor=lambda name, args: asyncio.sleep(0, result="found"))
            self.assertEqual(answer, "Done")
            self.assertEqual(requests[0]["system"], "You help")
            self.assertEqual(requests[0]["model"], "claude-opus-5-5")
        asyncio.run(run())

    def test_codex_api_uses_responses_and_preserves_function_call_id(self):
        requests = []

        def handler(request):
            self.assertEqual(request.url.path, "/v1/responses")
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(200, json={"output": [{"type": "reasoning", "encrypted_content": "opaque"},
                                                            {"type": "function_call", "call_id": "call-1",
                                                             "name": "lookup", "arguments": "{}"}]})
            self.assertEqual(body["input"][-1]["call_id"], "call-1")
            self.assertEqual(body["input"][-3]["encrypted_content"], "opaque")
            return httpx.Response(200, json={"output": [{"type": "message", "content": [
                {"type": "output_text", "text": "Ready"}]}]})

        async def run():
            with patch.object(openai_compatible.httpx, "AsyncClient", side_effect=lambda **kw: client_for(handler)):
                answer = await openai_compatible.run_turn(
                    "https://api.openai.com/v1", "gpt-5-codex", "test-key",
                    [{"role": "user", "content": "Hi"}],
                    tools=[{"type": "function", "function": {"name": "lookup", "description": "Find",
                             "parameters": {"type": "object", "properties": {}}}}],
                    tool_executor=lambda name, args: asyncio.sleep(0, result="found"))
            self.assertEqual(answer, "Ready")
            self.assertEqual(requests[0]["model"], "gpt-5-codex")
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
