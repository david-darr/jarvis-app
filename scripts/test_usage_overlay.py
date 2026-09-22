"""Isolated quota normalization, stale-state and route checks.
Run: .venv/Scripts/python.exe scripts/test_usage_overlay.py
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

data_dir = Path(__file__).resolve().parents[1] / "data"
data_dir.mkdir(exist_ok=True)
os.environ["JARVIS_DATA_DIR"] = str(data_dir)
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

    def test_route_requires_admin(self):
        app = FastAPI()
        app.include_router(model_routes.router)
        with TestClient(app) as client:
            with patch.object(middleware, "auth_enabled", return_value=True):
                self.assertEqual(client.get("/api/models/quotas").status_code, 401)
            app.dependency_overrides[require_admin] = lambda: "admin"
            with patch.object(quota_usage, "get_usage_overlay_async", return_value={"providers": [], "recorded": []}):
                self.assertEqual(client.get("/api/models/quotas").json(), {"providers": [], "recorded": []})


if __name__ == "__main__":
    unittest.main()
