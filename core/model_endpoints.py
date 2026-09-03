"""Model endpoint registry — user-configured "bring your own model" connections
(a local vLLM/Ollama/LM Studio server, a hosted OpenAI-compatible API like
OpenRouter/OpenAI itself, or the Claude Agent SDK/CLI). Mirrors Odysseus's
core.database.ModelEndpoint (name/base_url/api_key/model list), JSON-backed
here to match this project's storage convention rather than a SQL table.

David's ask 2026-08-31: JARVIS ships with NO default model. `local`/`api`
endpoints are treated as a plain OpenAI-compatible chat-completions API — one
client (core/providers/openai_compatible.py) handles all of them, no
per-vendor SDK. `claude_cli` is different: it's the Claude Agent SDK path
(core/brain.py, a tool-using agent with vault file access, not just a chat
endpoint) wrapping the real `claude` CLI already logged in on the machine —
no base_url or api_key needed, since that auth lives outside this app
entirely. A chat session with no endpoint chosen at all gets a canned
"add a model first" reply instead of silently falling back to any one of
these (see services/chat_service.py).
"""
import os
import uuid
from typing import Any, Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from core.secret_storage import decrypt, encrypt

ENDPOINTS_FILE = os.path.join(DATA_DIR, "model_endpoints.json")

DEFAULT_LOCAL_NUM_CTX = 4096  # real incident, 2026-09-01: a local model loaded
# with no cap defaulted to its max supported context (phi3's 131072) and its
# KV cache alone ate ~21GB of system RAM. Every "local" endpoint gets a sane
# cap by default instead of trusting the serving engine's own default.


def _load() -> dict:
    """Self-healing backfill (real bug found live, 2026-09-01 — David's own
    "Ollama" and "phi3:latest" endpoints, created before num_ctx capping
    existed, had no num_ctx field at all, so the cap silently never applied
    to them and memory ballooned exactly like the original incident): any
    "local" endpoint missing num_ctx gets the default written in immediately
    on load, not just newly-created ones. Runs on every load so it fixes
    itself the next time anything touches the file, regardless of when this
    code shipped relative to when the endpoint was created."""
    data = read_json(ENDPOINTS_FILE, {})
    changed = False
    for ep in data.values():
        if ep.get("kind") == "local" and ep.get("num_ctx") is None:
            ep["num_ctx"] = DEFAULT_LOCAL_NUM_CTX
            changed = True
    if changed:
        write_json_atomic(ENDPOINTS_FILE, data)
    return data


def _masked(ep: dict) -> dict:
    return {
        "id": ep["id"],
        "name": ep["name"],
        "base_url": ep["base_url"],
        "model": ep["model"],
        "has_api_key": bool(ep.get("api_key_encrypted")),
        "kind": ep.get("kind", "api"),
        "num_ctx": ep.get("num_ctx"),
    }


def list_endpoints() -> list[dict]:
    return [_masked(ep) for ep in _load().values()]


def get_endpoint(endpoint_id: str) -> Optional[dict]:
    return _load().get(endpoint_id)


def create_endpoint(name: str, base_url: str = "", model: str = "", api_key: Optional[str] = None,
                     kind: str = "api", num_ctx: Optional[int] = None) -> dict:
    if kind not in ("local", "api", "claude_cli"):
        raise ValueError("kind must be 'local', 'api', or 'claude_cli'")
    if kind != "claude_cli" and not base_url:
        raise ValueError("base_url is required for local/api endpoints")
    data = _load()
    endpoint_id = uuid.uuid4().hex[:12]
    data[endpoint_id] = {
        "id": endpoint_id,
        "name": name,
        "base_url": base_url.rstrip("/") if base_url else "",
        # For claude_cli, `model` is an optional override passed straight to
        # ClaudeAgentOptions(model=...) — blank means "whatever the `claude`
        # CLI itself defaults to" (frontend displays that as "CLI default").
        "model": model,
        "api_key_encrypted": encrypt(api_key) if api_key else None,
        "kind": kind,
        # Only meaningful for "local" — the serving engine's own context-
        # window cap (Ollama's `options.num_ctx`; other local servers that
        # don't recognize the field just ignore it). None for api/claude_cli.
        "num_ctx": (num_ctx if num_ctx is not None else DEFAULT_LOCAL_NUM_CTX) if kind == "local" else None,
    }
    write_json_atomic(ENDPOINTS_FILE, data)
    return _masked(data[endpoint_id])


def delete_endpoint(endpoint_id: str) -> None:
    data = _load()
    data.pop(endpoint_id, None)
    write_json_atomic(ENDPOINTS_FILE, data)


def resolve_runtime(endpoint_id: str) -> tuple[str, str, Optional[str], Optional[int]]:
    """(base_url, model, api_key, num_ctx) with the key decrypted — for
    runtime calls only, never returned from an API route."""
    ep = get_endpoint(endpoint_id)
    if ep is None:
        raise ValueError(f"unknown model endpoint: {endpoint_id}")
    api_key = decrypt(ep["api_key_encrypted"]) if ep.get("api_key_encrypted") else None
    return ep["base_url"], ep["model"], api_key, ep.get("num_ctx")
