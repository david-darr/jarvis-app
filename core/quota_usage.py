"""Best-effort desktop quota readings. No credential or quota response is persisted.

Claude Code's OAuth usage URL is undocumented and can change. Codex uses the
documented app-server rate-limit request. Both represent account allowances,
not per-model or per-endpoint usage. API/local endpoints only have JARVIS's
recorded lifetime token totals, so they are deliberately a separate section.
"""
import asyncio
import json
import math
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from core import model_endpoints, token_usage

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
REFRESH_SECONDS = 300
_cache: dict[str, dict] = {}
_retry_after: dict[str, float] = {}


def _percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(max(0, min(100, float(value))), 1)


def _unix_time(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value) / 1000 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def _window(name, used, resets_at):
    percent = _percent(used)
    if percent is None:
        return None
    return {"name": name, "used_percent": percent, "resets_at": _unix_time(resets_at)}


def parse_claude_usage(data):
    """Normalize the current OAuth payload and its older top-level shape."""
    if not isinstance(data, dict):
        return []
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, list):
        limits = []
    by_kind = {item.get("kind"): item for item in limits if isinstance(item, dict)}
    session = by_kind.get("session") or data.get("five_hour") or {}
    weekly = by_kind.get("weekly_all") or data.get("seven_day") or {}
    windows = []
    for label, item in (("5-hour", session), ("Weekly", weekly)):
        if isinstance(item, dict):
            entry = _window(label, item.get("percent", item.get("utilization")), item.get("resets_at"))
            if entry:
                windows.append(entry)
    return windows


def parse_codex_limits(data):
    payload = data.get("rateLimits") or data
    buckets = payload.get("rateLimitsByLimitId") if isinstance(payload, dict) else None
    if isinstance(buckets, dict):
        payload = buckets.get("codex") or (payload if "primary" in payload else {})
    if not isinstance(payload, dict):
        return []
    windows = []
    for key, label in (("primary", "5-hour"), ("secondary", "Weekly")):
        part = payload.get(key)
        if not isinstance(part, dict):
            continue
        duration = part.get("windowDurationMins")
        actual_label = label if duration in (300, 10080, None) else f"{duration}-minute"
        entry = _window(actual_label, part.get("usedPercent"), part.get("resetsAt"))
        if entry:
            windows.append(entry)
    return windows


def _claude_windows():
    path = Path.home() / ".claude" / ".credentials.json"
    credentials = json.loads(path.read_text(encoding="utf-8"))
    oauth = credentials.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires = _unix_time(oauth.get("expiresAt"))
    if not token or (expires is not None and expires <= time.time()):
        raise RuntimeError("Claude Code sign-in needs refresh")
    with httpx.Client(timeout=12) as client:
        response = client.get(CLAUDE_USAGE_URL, headers={
            "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        })
    if response.status_code == 429:
        _retry_after["claude"] = time.monotonic() + 300
        raise RuntimeError("Claude usage is rate limited")
    response.raise_for_status()
    return parse_claude_usage(response.json())


def _kill_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       capture_output=True, timeout=5, check=False)
    else:
        process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()


def _codex_windows():
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("Codex CLI is unavailable")
    process = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", bufsize=1,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lines = queue.Queue()
    def read_lines():
        for line in process.stdout:
            lines.put(line)
    threading.Thread(target=read_lines, daemon=True).start()
    try:
        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "jarvis-usage", "version": "1.0.0"}, "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
        ):
            process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                message = json.loads(lines.get(timeout=max(0.01, deadline - time.monotonic())))
            except (ValueError, queue.Empty):
                continue
            if message.get("id") == 2:
                if "error" in message:
                    raise RuntimeError("Codex rate limits unavailable")
                return parse_codex_limits(message.get("result") or {})
        raise RuntimeError("Codex rate limits timed out")
    finally:
        _kill_tree(process)


def _provider(name, reader):
    now = time.monotonic()
    old = _cache.get(name)
    if old and now - old["checked"] < REFRESH_SECONDS:
        return {**old["value"], "stale": False}
    if now < _retry_after.get(name, 0):
        return {**old["value"], "stale": True} if old else {"provider": name, "windows": [], "stale": True, "status": "Rate limited"}
    try:
        windows = reader()
        if not windows:
            raise RuntimeError("No quota windows returned")
        value = {"provider": name, "windows": windows, "updated_at": time.time(), "stale": False}
        _cache[name] = {"checked": now, "value": value}
        return value
    except Exception:
        # Exceptions from HTTP and subprocesses may contain URLs or account
        # data. Keep the public response deliberately generic.
        _retry_after[name] = max(_retry_after.get(name, 0), now + 60)
        return {**old["value"], "stale": True} if old else {"provider": name, "windows": [], "stale": True, "status": "Unavailable"}


def get_usage_overlay():
    endpoints = model_endpoints.list_endpoints()
    kinds = {endpoint["kind"] for endpoint in endpoints}
    providers = []
    if "claude_cli" in kinds:
        providers.append(_provider("claude", _claude_windows))
    if "codex_cli" in kinds:
        providers.append(_provider("codex", _codex_windows))
    totals = token_usage.get_usage_summary()
    recorded = [{"name": ep["name"], "kind": ep["kind"],
                 "total_tokens": totals.get(ep["id"], {}).get("total_tokens")}
                for ep in endpoints if ep["kind"] in ("api", "local")]
    return {"providers": providers, "recorded": recorded}


async def get_usage_overlay_async():
    return await asyncio.to_thread(get_usage_overlay)
