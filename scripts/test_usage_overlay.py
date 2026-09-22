"""Isolated quota normalization, stale-state and route checks.
Run: .venv/Scripts/python.exe scripts/test_usage_overlay.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# A throwaway data directory: this suite must never touch the real data/ folder.
_fixture = tempfile.TemporaryDirectory(prefix="jarvis-usage-overlay-test-")
os.environ["JARVIS_DATA_DIR"] = _fixture.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from core import quota_usage
from core import middleware
from core.middleware import require_admin
from routes import model_routes


class QuotaUsageTests(unittest.TestCase):
    def setUp(self):
        quota_usage._cache.clear()
        quota_usage._retry_after.clear()

    def test_claude_current_and_fallback_shapes(self):
        data = {"limits": [
            {"kind": "session", "percent": 23.4, "resets_at": "2026-09-23T00:00:00Z"},
            {"kind": "weekly_all", "percent": 51, "resets_at": "2026-09-27T00:00:00Z"},
        ], "five_hour": {"utilization": 99, "resets_at": "2026-09-23T00:00:00Z"}}
        windows = quota_usage.parse_claude_usage(data)
        self.assertEqual([w["name"] for w in windows], ["5-hour", "Weekly"])
        self.assertEqual(windows[0]["used_percent"], 23.4)
        self.assertIsInstance(windows[0]["resets_at"], float)
        self.assertEqual(quota_usage.parse_claude_usage({"five_hour": {"utilization": 8}})[0]["used_percent"], 8)

    def test_codex_bucket_and_timestamp(self):
        data = {"rateLimits": {"rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 1_800_000_000},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1_900_000_000},
        }}}}
        windows = quota_usage.parse_codex_limits(data)
        self.assertEqual([w["name"] for w in windows], ["5-hour", "Weekly"])
        self.assertEqual(windows[1]["resets_at"], 1_900_000_000)
        self.assertEqual(quota_usage.parse_codex_limits({"rateLimitsByLimitId": {
            "spark": {"primary": {"usedPercent": 99, "resetsAt": 1_800_000_000}}
        }}), [])

    def test_cache_stale_and_no_credentials_in_errors(self):
        quota_usage._provider("claude", lambda: [{"name": "5-hour", "used_percent": 20, "resets_at": None}])
        quota_usage._cache["claude"]["checked"] -= quota_usage.REFRESH_SECONDS + 1
        result = quota_usage._provider("claude", lambda: (_ for _ in ()).throw(RuntimeError("secret-token")))
        self.assertTrue(result["stale"])
        self.assertNotIn("secret-token", str(result))
        self.assertEqual(result["windows"][0]["used_percent"], 20)

    def test_api_and_local_are_totals_only_and_cli_account_is_deduplicated(self):
        endpoints = [
            {"id": "c1", "name": "Claude Opus", "kind": "claude_cli"},
            {"id": "c2", "name": "Claude Sonnet", "kind": "claude_cli"},
            {"id": "a1", "name": "API", "kind": "api"},
            {"id": "l1", "name": "Local", "kind": "local"},
        ]
        with patch.object(quota_usage.model_endpoints, "list_endpoints", return_value=endpoints), \
             patch.object(quota_usage.token_usage, "get_usage_summary", return_value={"a1": {"total_tokens": 123}}), \
             patch.object(quota_usage, "_provider", return_value={"provider": "claude", "windows": []}) as provider:
            result = quota_usage.get_usage_overlay()
        provider.assert_called_once()
        self.assertEqual(result["recorded"], [
            {"name": "API", "kind": "api", "total_tokens": 123},
            {"name": "Local", "kind": "local", "total_tokens": None},
        ])

    def test_each_failure_says_what_kind_it_was_without_leaking_text(self):
        def refused():
            raise quota_usage.SignInNeeded("token abc123 expired")
        def limited():
            raise quota_usage.RateLimited("429 from https://internal.example/?t=abc123")
        def broken():
            raise RuntimeError("secret-token in a traceback")
        for reader, status in ((refused, "needs_sign_in"), (limited, "rate_limited"), (broken, "unavailable")):
            with self.subTest(status=status):
                quota_usage._cache.clear(); quota_usage._retry_after.clear()
                result = quota_usage._provider("claude", reader)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["note"], quota_usage.NOTES[status])
                self.assertNotIn("abc123", str(result)); self.assertNotIn("secret-token", str(result))

    def test_a_click_reads_now_but_not_twice_in_a_row(self):
        calls = []
        def reader():
            calls.append(1)
            return [{"name": "5-hour", "used_percent": len(calls) * 10, "resets_at": None}]
        quota_usage._provider("codex", reader)
        self.assertEqual(quota_usage._provider("codex", reader)["windows"][0]["used_percent"], 10, "cached, not re-read")
        with self.assertRaises(quota_usage.RefreshThrottled):
            quota_usage._provider("codex", reader, force=True)
        quota_usage._cache["codex"]["checked"] -= quota_usage.FORCED_REFRESH_SECONDS + 1
        self.assertEqual(quota_usage._provider("codex", reader, force=True)["windows"][0]["used_percent"], 20)
        self.assertEqual(len(calls), 2)

    def test_refresh_route_rejects_unknown_providers_and_throttles(self):
        app = FastAPI()
        app.include_router(model_routes.router)
        app.dependency_overrides[require_admin] = lambda: "admin"
        with TestClient(app) as client:
            self.assertEqual(client.post("/api/models/quotas/refresh", json={"provider": "cursor"}).status_code, 400)
            with patch.object(quota_usage, "get_usage_overlay_async", side_effect=quota_usage.RefreshThrottled("claude")):
                self.assertEqual(client.post("/api/models/quotas/refresh", json={"provider": "claude"}).status_code, 429)

    def test_route_requires_admin(self):
        app = FastAPI()
        app.include_router(model_routes.router)
        with TestClient(app) as client:
            with patch.object(middleware, "auth_enabled", return_value=True):
                self.assertEqual(client.get("/api/models/quotas").status_code, 401)
            app.dependency_overrides[require_admin] = lambda: "admin"
            with patch.object(quota_usage, "get_usage_overlay_async", return_value={"providers": [], "recorded": []}):
                self.assertEqual(client.get("/api/models/quotas").json(), {"providers": [], "recorded": []})


class ModelMarkTests(unittest.TestCase):
    """Each added model shows its provider's logo, or none: a wrong logo is
    worse than the generic icon."""

    def test_provider_is_recognised_from_kind_model_and_host(self):
        from core.model_marks import mark_for
        cases = {
            "claude": [{"kind": "claude_cli"}, {"kind": "api", "base_url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-3.5-sonnet"}],
            "openai": [{"kind": "codex_cli"}, {"kind": "api", "base_url": "https://api.openai.com/v1", "model": "gpt-5"}],
            "grok": [{"kind": "api", "base_url": "https://api.x.ai/v1", "model": "grok-4"}],
            "perplexity": [{"kind": "api", "base_url": "https://api.perplexity.ai", "model": "sonar-pro"}],
            "deepseek": [{"kind": "api", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"}],
            "gemini": [{"kind": "api", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-pro"}],
            "meta": [{"kind": "local", "base_url": "http://localhost:11434/v1", "model": "llama3.1:8b"}],
            "qwen": [{"kind": "local", "base_url": "http://localhost:11434/v1", "model": "Qwen 3.8"}],
            "ollama": [{"kind": "local", "base_url": "http://localhost:11434/v1", "model": "mystery-model"}],
            "openrouter": [{"kind": "api", "base_url": "https://openrouter.ai/api/v1", "model": "vendor/unknown"}],
        }
        for mark, endpoints in cases.items():
            for endpoint in endpoints:
                with self.subTest(endpoint=endpoint):
                    self.assertEqual(mark_for(endpoint), mark)

    def test_no_match_is_no_logo_not_a_wrong_one(self):
        from core.model_marks import mark_for
        for endpoint in ({"kind": "api", "base_url": "https://example.com/v1", "model": "graphite"},
                         {"kind": "api", "base_url": "https://evilx.ai.example.com", "model": "helper"},
                         {"kind": "api", "base_url": "not a url", "model": ""}):
            with self.subTest(endpoint=endpoint):
                self.assertIsNone(mark_for(endpoint))

    def test_every_mark_has_its_logo_file_and_the_files_are_inert(self):
        from core.model_marks import MARKS
        folder = Path(__file__).resolve().parents[1] / "static" / "img" / "model-marks"
        self.assertTrue((folder / "NOTICE.md").exists())
        for mark in MARKS:
            with self.subTest(mark=mark):
                svg = (folder / f"{mark}.svg").read_text(encoding="utf-8")
                self.assertIn('fill="currentColor"', svg)
                self.assertNotRegex(svg.lower(), r"<script|on[a-z]+=|href=")

    def test_the_model_list_carries_each_models_mark(self):
        app = FastAPI()
        app.include_router(model_routes.router)
        app.dependency_overrides[require_admin] = lambda: "admin"
        endpoints = [{"id": "a", "name": "Grok", "kind": "api", "base_url": "https://api.x.ai/v1", "model": "grok-4"},
                     {"id": "b", "name": "Mine", "kind": "api", "base_url": "https://example.com", "model": "x"}]
        with TestClient(app) as client, patch.object(model_routes.model_endpoints, "list_endpoints", return_value=endpoints):
            self.assertEqual([e["mark"] for e in client.get("/api/models").json()], ["grok", None])


if __name__ == "__main__":
    unittest.main()
