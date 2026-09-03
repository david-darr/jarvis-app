"""Cookbook: browse/pull/manage local Ollama models (David's ask 2026-09-01).
See core/ollama_client.py's module docstring for why this wraps Ollama's own
API instead of reimplementing a serving engine.

Pulls run as a background asyncio task so the request that starts one
returns immediately; progress is tracked in-memory (process-lifetime only —
a restart mid-pull loses progress tracking, though the download itself is
Ollama's own resumable pull under the hood, not something this process
owns) and polled by the frontend, same pattern as core/task_scheduler.py's
background polling rather than a websocket/SSE push.
"""
import asyncio

from core import ollama_client

# A curated list, not a live catalog search — Ollama has no public "search
# models" API without scraping ollama.com's library pages, which is heavier
# and more fragile than this app needs. Honest about being curated, not
# pretending to be exhaustive or live.
CATALOG = [
    {"name": "llama3.2", "label": "Llama 3.2", "params": "3B", "description": "Meta's small, fast general-purpose model — good default for a local machine."},
    {"name": "llama3.1", "label": "Llama 3.1", "params": "8B", "description": "Larger Llama, stronger reasoning, needs more RAM/VRAM."},
    {"name": "mistral", "label": "Mistral", "params": "7B", "description": "Fast, capable general-purpose model."},
    {"name": "qwen2.5", "label": "Qwen 2.5", "params": "7B", "description": "Strong general + coding performance."},
    {"name": "qwen2.5-coder", "label": "Qwen 2.5 Coder", "params": "7B", "description": "Coding-specialized variant of Qwen 2.5."},
    {"name": "deepseek-r1", "label": "DeepSeek R1", "params": "7B", "description": "Reasoning-focused model with visible chain-of-thought."},
    {"name": "phi3", "label": "Phi-3", "params": "3.8B", "description": "Microsoft's small model, strong for its size."},
    {"name": "gemma2", "label": "Gemma 2", "params": "9B", "description": "Google's open model, solid general performance."},
    {"name": "codellama", "label": "Code Llama", "params": "7B", "description": "Meta's coding-specialized Llama variant."},
    {"name": "nomic-embed-text", "label": "Nomic Embed Text", "params": "137M", "description": "Small embedding model, not for chat — useful for search/RAG if that's ever built."},
]

_pull_progress: dict[str, dict] = {}


def get_catalog() -> list[dict]:
    return CATALOG


async def get_status(base_url: str = ollama_client.DEFAULT_BASE_URL) -> dict:
    reachable = await ollama_client.is_reachable(base_url)
    return {"reachable": reachable}


async def get_installed(base_url: str = ollama_client.DEFAULT_BASE_URL) -> list[dict]:
    return await ollama_client.list_installed(base_url)


async def get_running(base_url: str = ollama_client.DEFAULT_BASE_URL) -> list[dict]:
    return await ollama_client.list_running(base_url)


async def delete_model(name: str, base_url: str = ollama_client.DEFAULT_BASE_URL) -> None:
    await ollama_client.delete_model(name, base_url)
    _pull_progress.pop(name, None)


def get_pull_progress(name: str) -> dict:
    return _pull_progress.get(name, {"status": "not_started"})


def start_pull(name: str, base_url: str = ollama_client.DEFAULT_BASE_URL) -> None:
    if _pull_progress.get(name, {}).get("status") == "pulling":
        return  # already in progress — don't start a second one
    _pull_progress[name] = {"status": "pulling", "completed": 0, "total": 0, "done": False, "error": None}
    asyncio.create_task(_run_pull(name, base_url))


async def _run_pull(name: str, base_url: str) -> None:
    try:
        async for chunk in ollama_client.pull_stream(name, base_url):
            entry = _pull_progress.get(name, {})
            entry["status"] = chunk.get("status", entry.get("status"))
            if "completed" in chunk:
                entry["completed"] = chunk["completed"]
            if "total" in chunk:
                entry["total"] = chunk["total"]
            _pull_progress[name] = entry
        _pull_progress[name] = {**_pull_progress.get(name, {}), "status": "success", "done": True}
    except Exception as e:
        _pull_progress[name] = {**_pull_progress.get(name, {}), "status": "error", "done": True, "error": str(e)[:300]}
