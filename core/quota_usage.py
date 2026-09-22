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
# A click on a ring forces a fresh reading, but never more often than this per
# provider: the Claude endpoint rate-limits, and a click is not worth a 429.
FORCED_REFRESH_SECONDS = 20
_cache: dict[str, dict] = {}
_retry_after: dict[str, float] = {}


class SignInNeeded(RuntimeError):
    """The CLI's own sign-in is missing, expired or refused."""


class RateLimited(RuntimeError):
    """The provider answered 429."""


class RefreshThrottled(RuntimeError):
    """A forced refresh arrived sooner than FORCED_REFRESH_SECONDS."""


# Fixed wording only. Exceptions from HTTP and subprocesses can carry URLs or
# account data, so none of their text ever reaches the response.
NOTES = {
    "needs_sign_in": "Sign-in has expired or is missing. Open the CLI once to renew it.",
    "rate_limited": "The provider asked for a pause. Retrying shortly.",
    "unavailable": "Usage could not be read right now.",
}


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
        raise SignInNeeded("Claude Code sign-in needs refresh")
    with httpx.Client(timeout=12) as client:
        response = client.get(CLAUDE_USAGE_URL, headers={
            "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        })
    if response.status_code == 429:
        _retry_after["claude"] = time.monotonic() + 300
        raise RateLimited("Claude usage is rate limited")
    if response.status_code == 401:
        raise SignInNeeded("Claude Code sign-in was refused")
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


def _failed(name, old, status):
    if old:
        return {**old["value"], "stale": True, "status": "stale", "note": NOTES[status]}
    return {"provider": name, "windows": [], "stale": True, "status": status, "note": NOTES[status]}


def _provider(name, reader, force=False):
    """One provider's reading: cached for REFRESH_SECONDS, or fetched now when
    `force` is set (a click on its ring). Every result carries a `status` -
    ok, stale, needs_sign_in, rate_limited or unavailable - and a fixed note."""
    now = time.monotonic()
    old = _cache.get(name)
    if force and old and now - old["checked"] < FORCED_REFRESH_SECONDS:
        raise RefreshThrottled(name)
    if old and not force and now - old["checked"] < REFRESH_SECONDS:
        return {**old["value"], "stale": False}
    if now < _retry_after.get(name, 0):
        return _failed(name, old, "rate_limited")
    try:
        windows = reader()
        if not windows:
            raise RuntimeError("No quota windows returned")
        value = {"provider": name, "status": "ok", "note": "", "windows": windows,
                 "updated_at": time.time(), "stale": False}
        _cache[name] = {"checked": now, "value": value}
        return value
    except SignInNeeded:
        # Sign-in problems are not retried on a timer: nothing changes until
        # the person signs in again, which the next ordinary poll will see.
        return {"provider": name, "windows": [], "stale": True, "status": "needs_sign_in", "note": NOTES["needs_sign_in"]}
    except RateLimited:
        return _failed(name, old, "rate_limited")
    except Exception:
        _retry_after[name] = max(_retry_after.get(name, 0), now + 60)
        return _failed(name, old, "unavailable")


READERS = {"claude": ("claude_cli", lambda: _claude_windows()), "codex": ("codex_cli", lambda: _codex_windows())}


def get_usage_overlay(force_provider=None):
    """Readings for every subscription CLI endpoint that is connected.
    `force_provider` refreshes that one now (raises RefreshThrottled if it was
    read moments ago); the rest come from cache as usual."""
    endpoints = model_endpoints.list_endpoints()
    kinds = {endpoint["kind"] for endpoint in endpoints}
    providers = []
    for name, (kind, reader) in READERS.items():
        if kind in kinds:
            providers.append(_provider(name, reader, force=(name == force_provider)))
    totals = token_usage.get_usage_summary()
    recorded = [{"name": ep["name"], "kind": ep["kind"],
                 "total_tokens": totals.get(ep["id"], {}).get("total_tokens")}
                for ep in endpoints if ep["kind"] in ("api", "local")]
    return {"providers": providers, "recorded": recorded}


async def get_usage_overlay_async(force_provider=None):
    return await asyncio.to_thread(get_usage_overlay, force_provider)
