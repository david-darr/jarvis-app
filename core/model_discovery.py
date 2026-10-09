"""Live, connection-scoped model discovery for chat pickers.

The cache is intentionally short-lived. A picker request after a release should
see the provider's current list without requiring a Kairos update or restart.
Failed probes keep the last successful result; no credential is returned to the
browser or written to this cache.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
import platform
import shutil
import subprocess
import time
from urllib.parse import urlparse

import httpx

from core import model_catalog
from core.secret_storage import decrypt

_TTL_SECONDS = 300
_MAX_MODELS = 1000
_cache: dict[str, tuple[float, list[dict]]] = {}


def provider_for_endpoint(endpoint: dict) -> str | None:
    kind = endpoint.get("kind")
    if kind == "claude_cli":
        return "anthropic"
    if kind == "codex_cli":
        return "codex"
    if kind != "api":
        return None
    parsed = urlparse(endpoint.get("base_url") or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return None
    if host == "api.anthropic.com":
        return "anthropic"
    if host == "api.openai.com":
        return "openai"
    return None


def _claude_cli_token() -> str | None:
    """Borrow the current access token, read-only. Claude Code owns refresh."""
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    path = os.path.join(root, ".credentials.json")
    candidates = []
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
            if result.returncode == 0 and result.stdout:
                candidates.append(json.loads(result.stdout))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    try:
        if os.path.getsize(path) <= 1024 * 1024:
            with open(path, encoding="utf-8") as stream:
                candidates.append(json.load(stream))
    except (OSError, ValueError):
        pass
    valid: list[tuple[float, str]] = []
    for data in candidates:
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if not isinstance(oauth, dict):
            continue
        expires_at = oauth.get("expiresAt")
        token = oauth.get("accessToken")
        if (isinstance(expires_at, (int, float)) and expires_at > (time.time() + 30) * 1000
                and isinstance(token, str) and token):
            valid.append((expires_at, token))
    return max(valid)[1] if valid else None


def _api_key(endpoint: dict) -> str | None:
    encrypted = endpoint.get("api_key_encrypted")
    return decrypt(encrypted) if encrypted else None


def _entry(model_id: str, name: str | None = None, *, source: str) -> dict:
    return {
        "id": model_id, "display_name": name or model_id, "description": "",
        "alias": None, "default_effort": None, "supported_efforts": [],
        "context_window": None, "effective_context_percent": None,
        "source": source, "estimated": False,
    }


async def _anthropic_models(token: str, *, oauth: bool) -> list[dict]:
    headers = {"anthropic-version": "2023-06-01"}
    if oauth:
        headers["Authorization"] = f"Bearer {token}"
        headers["anthropic-beta"] = "claude-code-20250219,oauth-2025-04-20"
    else:
        headers["x-api-key"] = token
    out: list[dict] = []
    cursor: str | None = None
    async with httpx.AsyncClient(timeout=5) as client:
        for _ in range(20):
            params = {"limit": 100, **({"after_id": cursor} if cursor else {})}
            response = await client.get("https://api.anthropic.com/v1/models", headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            for item in data.get("data", []):
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    out.append(_entry(item["id"], item.get("display_name"), source="anthropic_api"))
            if not data.get("has_more") or len(out) >= _MAX_MODELS:
                break
            next_cursor = data.get("last_id")
            if not isinstance(next_cursor, str) or next_cursor == cursor:
                break
            cursor = next_cursor
    return out[:_MAX_MODELS]


async def _openai_models(token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
    # /models has no chat capability flag. Exclude known single-purpose
    # families while allowing new naming schemes to appear automatically.
    non_chat = ("text-embedding-", "embedding-", "text-moderation-",
                "omni-moderation-", "dall-e-", "gpt-image-", "image-",
                "tts-", "whisper-", "gpt-4o-transcribe", "gpt-4o-mini-transcribe",
                "gpt-4o-mini-tts")
    out = []
    for item in response.json().get("data", []):
        model_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(model_id, str) and not model_id.startswith(non_chat):
            out.append(_entry(model_id, source="openai_api"))
    return sorted(out[:_MAX_MODELS], key=lambda m: m["id"], reverse=True)


async def _codex_cli_models() -> list[dict]:
    """Ask the installed CLI's app server, so discovery uses its own login."""
    from core.codex_cli import find_codex
    executable = find_codex()
    if not executable:
        return []
    process = await asyncio.create_subprocess_exec(
        executable, "app-server", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    async def send(message: dict) -> None:
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    async def response_for(request_id: int) -> dict:
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=8)
            if not line or len(line) > 1024 * 1024:
                return {}
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if payload.get("id") == request_id:
                return payload.get("result") or {}

    try:
        await send({"method": "initialize", "id": 1, "params": {"clientInfo": {
            "name": "jarvis_app", "title": "Kairos", "version": "1.0.0"}}})
        if not await response_for(1):
            return []
        await send({"method": "initialized", "params": {}})
        out: list[dict] = []
        cursor = None
        for page in range(20):
            params = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            request_id = page + 2
            await send({"method": "model/list", "id": request_id, "params": params})
            result = await response_for(request_id)
            for item in result.get("data") or []:
                model_id = item.get("model") or item.get("id")
                if not isinstance(model_id, str) or item.get("hidden"):
                    continue
                efforts = [{"effort": e["reasoningEffort"],
                            "description": e.get("description") or ""}
                           for e in item.get("supportedReasoningEfforts") or []
                           if isinstance(e, dict) and e.get("reasoningEffort")]
                entry = _entry(model_id, item.get("displayName"), source="cli_live")
                entry.update({"default_effort": item.get("defaultReasoningEffort"),
                              "supported_efforts": efforts, "context_window": item.get("contextWindow")})
                out.append(entry)
            cursor = result.get("nextCursor")
            if not cursor or len(out) >= _MAX_MODELS:
                break
        return out[:_MAX_MODELS]
    finally:
        if process.returncode is None:
            if os.name == "nt":
                # npm's codex.CMD is a parent launcher. Killing only it can
                # orphan codex.exe, as Kairos's chat timeout once did.
                try:
                    await asyncio.to_thread(subprocess.run,
                        ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                        capture_output=True, timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
            else:
                process.kill()
        await asyncio.wait_for(process.wait(), timeout=5)


async def list_for_endpoint(endpoint: dict, *, force: bool = False) -> list[dict]:
    provider = provider_for_endpoint(endpoint)
    kind = endpoint.get("kind")
    if kind == "codex_cli":
        cache_key = "codex_cli"
        cached = _cache.get(cache_key)
        if cached and not force and time.monotonic() - cached[0] < _TTL_SECONDS:
            return cached[1]
        try:
            live = await _codex_cli_models()
        except (OSError, ValueError, asyncio.TimeoutError):
            live = []
        if live:
            fallback_by_id = {m["id"]: m for m in model_catalog._load_codex_models()}
            for entry in live:
                cached = fallback_by_id.get(entry["id"])
                if cached and not entry.get("context_window"):
                    entry["context_window"] = cached.get("context_window")
                    entry["effective_context_percent"] = cached.get("effective_context_percent")
            model_catalog.remember_codex_live(live, _TTL_SECONDS)
            _cache[cache_key] = (time.monotonic(), live)
            return live
        return cached[1] if cached else model_catalog.list_models(kind)
    if provider is None:
        return []
    token = _claude_cli_token() if kind == "claude_cli" else _api_key(endpoint)
    fallback = model_catalog.list_models("claude_cli") if kind == "claude_cli" else []
    if not token:
        return fallback
    identity = hashlib.sha256(token.encode()).hexdigest()[:16]
    cache_key = f"{kind}:{provider}:{endpoint.get('id')}:{identity}"
    cached = _cache.get(cache_key)
    if cached and not force and time.monotonic() - cached[0] < _TTL_SECONDS:
        return cached[1]
    try:
        live = await (_anthropic_models(token, oauth=kind == "claude_cli")
                      if provider == "anthropic" else _openai_models(token))
    except (httpx.HTTPError, ValueError, TypeError):
        return cached[1] if cached else fallback
    if live:
        if kind == "claude_cli":
            # Alias rows preserve CLI-default-to-latest choices and SDK effort
            # levels, while exact live IDs are account-scoped provider data.
            ids = {m["id"] for m in live}
            live.extend(m for m in fallback if m["id"] not in ids)
        _cache[cache_key] = (time.monotonic(), live)
        return live
    return cached[1] if cached else fallback
