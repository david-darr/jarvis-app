"""Thin client for a local Ollama server's own REST API (default
http://localhost:11434) — the actual serving backend for jarvis-app's
Cookbook tab (David's ask 2026-09-01: "can we develop the cookbook tab
now"). Deliberately built on Ollama rather than reimplementing Odysseus's
own llama.cpp/vLLM download-and-serve pipeline (see specs/cookbook-hwfit.md
in ~/odysseus — raw process lifecycle, tmux, remote SSH, Docker GPU
passthrough, HF token management, PID handling across POSIX/Windows) —
Ollama already does model download/serve/hardware-fit itself, and it's the
local model server every other part of this project already points users
at (onboarding, Settings > Add Local Models placeholder, the v1 starter
kit's local_triage.py precedent). Cookbook's real job here is just a good
UI over Ollama's own API, not a second serving engine.
"""
import json
from typing import AsyncIterator

import httpx

DEFAULT_BASE_URL = "http://localhost:11434"
TIMEOUT_SECONDS = 15  # short — these are local-loopback calls, a hang means Ollama isn't there


async def is_reachable(base_url: str = DEFAULT_BASE_URL) -> bool:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{base_url}/api/tags")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def list_installed(base_url: str = DEFAULT_BASE_URL) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        resp = await client.get(f"{base_url}/api/tags")
        resp.raise_for_status()
        return resp.json().get("models", [])


async def list_running(base_url: str = DEFAULT_BASE_URL) -> list[dict]:
    """Currently loaded-into-memory models (Ollama's own /api/ps) — separate
    from "installed" (on disk but not necessarily loaded)."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        resp = await client.get(f"{base_url}/api/ps")
        resp.raise_for_status()
        return resp.json().get("models", [])


async def delete_model(name: str, base_url: str = DEFAULT_BASE_URL) -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        resp = await client.request("DELETE", f"{base_url}/api/delete", json={"name": name})
        resp.raise_for_status()


def is_ollama_url(base_url: str) -> bool:
    """Best-effort detection for "this endpoint is (probably) a local
    Ollama server" — checked by core/providers/openai_compatible.py before
    routing a context-capped call through chat_capped() below instead of
    the generic OpenAI-compatible path. Port 11434 is Ollama's real,
    consistently-used default; nothing else in this app's local-model story
    defaults to it."""
    return ":11434" in base_url


async def chat_capped(model: str, messages: list[dict], num_ctx: int, tools: list[dict] | None = None,
                       base_url: str = DEFAULT_BASE_URL) -> dict:
    """Real bug found live, 2026-09-01: Ollama 0.33.1's OpenAI-*compatible*
    `/v1/chat/completions` endpoint silently ignores `num_ctx` — neither
    nested under `options` (that's the native endpoint's shape) nor as a
    top-level field (which Ollama's own docs describe, but doesn't actually
    work on this installed version) changes the loaded context size. Only
    the *native* `/api/chat` endpoint honors `options.num_ctx` — confirmed
    directly: loading a model uncapped took ~21GB of VRAM for its KV cache
    alone (a real incident that started this whole fix); capped to 2048 via
    native /api/chat, the same model dropped to ~2.3GB.

    Returns an OpenAI-shaped response dict (`choices[0].message`, `usage`)
    so callers don't need two different response-parsing paths — this is
    the one place that translates Ollama's native shape into that one.
    Non-streaming only: this path exists specifically for the "cap memory"
    case, where correctness matters more than token-by-token streaming."""
    ollama_base = base_url.removesuffix("/v1").rstrip("/") or DEFAULT_BASE_URL
    body = {"model": model, "messages": messages, "stream": False, "options": {"num_ctx": num_ctx}}
    if tools:
        body["tools"] = tools
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(f"{ollama_base}/api/chat", json=body)
        resp.raise_for_status()
        data = resp.json()
    message = data.get("message", {})
    usage = {
        "prompt_tokens": data.get("prompt_eval_count", 0),
        "completion_tokens": data.get("eval_count", 0),
        "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
    }
    return {"choices": [{"message": message}], "usage": usage}


async def pull_stream(name: str, base_url: str = DEFAULT_BASE_URL) -> AsyncIterator[dict]:
    """Yields Ollama's own NDJSON progress objects
    (`{"status", "completed", "total", ...}`) as they arrive — a real
    streaming download, not a fake progress bar. No fixed timeout: a model
    pull can legitimately take many minutes on a slow connection."""
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", f"{base_url}/api/pull", json={"name": name, "stream": True}) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                yield json.loads(line)
