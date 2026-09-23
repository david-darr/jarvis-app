"""Generic OpenAI-compatible chat client — the one implementation every
"bring your own model" endpoint routes through (local vLLM/Ollama/LM Studio,
or a hosted API like OpenRouter/OpenAI), matching Odysseus's approach of
treating every registered endpoint as an OpenAI-compatible /chat/completions
API rather than writing a bespoke client per provider.

Real tool-calling loop added (David's ask 2026-08-31: shared memory +
cross-session awareness "out of the box for all imported AI models both
local and API") — uses the standard OpenAI `tools`/`tool_calls` function-
calling protocol, which most modern local (Ollama, LM Studio, vLLM) and
hosted OpenAI-compatible APIs support. Not every model does, though, so
this degrades gracefully: if the endpoint rejects the `tools` field outright
(some older/smaller local models 400 on an unrecognized param), it retries
once as a plain chat call with no tools rather than failing the whole turn.

num_ctx capping added 2026-09-01 (real incident — a local model loaded with
no cap defaulted to its max context window and its KV cache alone ate
~21GB of RAM). Real bug caught live before this shipped: Ollama 0.33.1's
OpenAI-*compatible* `/v1/chat/completions` silently ignores `num_ctx` in
every shape tried (nested under `options`, top-level per Ollama's own
docs — neither actually changed the loaded context size, verified directly
against a running Ollama instance). Only Ollama's *native* `/api/chat`
endpoint honors it. `_post_chat()` below is the one place that decides:
a detected local Ollama endpoint (core/ollama_client.is_ollama_url) with
num_ctx set routes through `ollama_client.chat_capped()` instead of the
generic HTTP path; everything else (non-Ollama local server, hosted API, or
no num_ctx set) is unaffected and still gets a harmless top-level
`num_ctx` field sent (ignored by servers that don't recognize it).

Fake-tool-call rescue added 2026-09-01 (real incident — a small local test
model, given the hive-mind memory tools, replied with a plain-text JSON
blob shaped exactly like a tool call — `{"name": "search_sessions",
"arguments": {...}}` — instead of using the API's real structured
`tool_calls` field). Some smaller/weaker models understand the *concept*
of calling a tool well enough to produce that shape, but aren't reliable
about actually using the dedicated field for it. `_extract_fake_tool_call()`
below detects that exact pattern (content is a JSON object, its "name" is
one of the tools actually offered this turn — not just anything JSON-
shaped) and rescues it into a real tool call so it actually executes,
instead of the user seeing raw JSON as if it were the model's answer.

Tool rounds kept, 2026-09-22 (prompt-cache audit finding 2): a caller can
pass `rounds`, a list this module appends each tool-calling assistant
message and each tool result to as they happen - the very same dicts sent
in the request. The caller keeps them in its history, so the next turn's
request extends this one exactly (the provider's prompt cache carries over)
and the model still has its earlier tool results. They used to exist only
in this module's local copy of the history and were dropped after every
turn. Appended as they happen, not at the end, so a stopped turn still
records the tools that actually ran.
"""
import json
from typing import Awaitable, AsyncIterator, Callable, Optional
from urllib.parse import urlparse

import httpx

from core import ollama_client

TIMEOUT_SECONDS = 120
MAX_TOOL_ROUNDS = 4  # bounded so a model that keeps calling tools can't loop forever


def _headers(api_key: Optional[str]) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _parse_tool_arguments(raw) -> dict:
    """Real bug found live, 2026-09-01: strict OpenAI spec requires
    `function.arguments` to be a JSON-*encoded string*, but Ollama's native
    tool-calling returns it as an actual dict/object directly — a real
    divergence from the spec, not a hypothetical. Handles both rather than
    assuming the spec-compliant shape and crashing on Ollama's real one."""
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_fake_tool_call(content: Optional[str], tools: Optional[list[dict]]) -> Optional[dict]:
    """See module docstring. Returns an OpenAI-shaped tool_calls entry if
    `content` is a JSON object naming one of this turn's real tools, else
    None. Deliberately strict — only a known tool name matches, so a real
    answer that happens to start/end with braces (e.g. describing a JSON
    file) is never misread as a tool call."""
    if not tools or not content:
        return None
    stripped = content.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    name, args = obj.get("name"), obj.get("arguments")
    if not isinstance(name, str) or not isinstance(args, dict):
        return None
    known_names = {t["function"]["name"] for t in tools if t.get("type") == "function"}
    if name not in known_names:
        return None
    return {"id": f"rescued-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _without_tool_rounds(messages: list[dict]) -> list[dict]:
    """The text-only view of a history, for the no-tools fallback below: an
    endpoint that rejects the `tools` field may reject tool-role messages
    too, and a fallback that used to work must not start failing because
    the history now carries tool rounds."""
    plain = []
    for m in messages:
        if m.get("role") == "tool":
            continue
        if m.get("tool_calls"):
            if m.get("content"):
                plain.append({"role": m["role"], "content": m["content"]})
            continue
        plain.append(m)
    return plain


# Prompt-cache audit finding 5 (2026-09-22), laid out as Hermes's
# agent/prompt_caching.py does for envelope routes. OpenAI, DeepSeek and local
# servers reuse a cached prefix on their own; an Anthropic model reached through
# OpenRouter caches only where the request carries a cache_control marker, and
# JARVIS sent none, so every turn re-billed the whole conversation.
_CACHE_MARKER = {"type": "ephemeral"}
_MAX_CACHE_MARKERS = 4  # the most one Anthropic request accepts


def _needs_cache_markers(base_url: str, model: str) -> bool:
    host = (urlparse(base_url or "").hostname or "").lower()
    on_openrouter = host == "openrouter.ai" or host.endswith(".openrouter.ai")
    return on_openrouter and "claude" in (model or "").lower()


def _can_carry_marker(message: dict) -> bool:
    # Only inside a content part: OpenRouter hangs on a marker on a role:tool
    # envelope and ignores one on an empty assistant turn, so a message with no
    # text (a tool call) gets none rather than wasting one of the four.
    content = message.get("content")
    if isinstance(content, str):
        return content != ""
    return isinstance(content, list) and bool(content) and isinstance(content[-1], dict)


def _marked(message: dict) -> dict:
    content = message["content"]
    if isinstance(content, str):
        parts = [{"type": "text", "text": content, "cache_control": dict(_CACHE_MARKER)}]
    else:
        parts = [*content[:-1], {**content[-1], "cache_control": dict(_CACHE_MARKER)}]
    return {**message, "content": parts}


def _request_messages(base_url: str, model: str, messages: list[dict]) -> list[dict]:
    """The messages as this request sends them. Unchanged except for an
    Anthropic model on OpenRouter, which gets a marker on the system message
    (covering the tool list too, which renders before it) and on each of the
    last three messages, so every turn reads what the previous one cached.
    A copy: markers never reach the stored history, which stays
    byte-comparable from turn to turn."""
    if not _needs_cache_markers(base_url, model):
        return messages
    marked = list(messages)
    used = 0
    if marked and marked[0].get("role") == "system" and _can_carry_marker(marked[0]):
        marked[0] = _marked(marked[0])
        used = 1
    carriers = [i for i, m in enumerate(marked) if m.get("role") != "system" and _can_carry_marker(m)]
    for i in carriers[-(_MAX_CACHE_MARKERS - used):]:
        marked[i] = _marked(marked[i])
    return marked


def _record(working_messages: list[dict], rounds: Optional[list[dict]], message: dict) -> None:
    working_messages.append(message)
    if rounds is not None:
        rounds.append(message)


async def _post_chat(client: httpx.AsyncClient, base_url: str, api_key: Optional[str], body: dict) -> dict:
    """The one place num_ctx capping actually gets applied — see module
    docstring. `body` may carry a `num_ctx` key; it's popped here rather
    than left in the JSON sent to non-Ollama servers verbatim, since this
    function is the boundary that decides which transport handles it."""
    num_ctx = body.pop("num_ctx", None)
    if num_ctx and ollama_client.is_ollama_url(base_url):
        return await ollama_client.chat_capped(
            body["model"], body["messages"], num_ctx, tools=body.get("tools"), base_url=base_url,
        )
    if num_ctx:
        body["num_ctx"] = num_ctx  # harmless best-effort for non-Ollama servers
    resp = await client.post(f"{base_url}/chat/completions", headers=_headers(api_key), json=body)
    resp.raise_for_status()
    return resp.json()


async def run_turn(base_url: str, model: str, api_key: Optional[str], messages: list[dict],
                    tools: Optional[list[dict]] = None, tool_executor: Optional[Callable[[str, dict], Awaitable[str]]] = None,
                    on_usage: Optional[Callable[[dict], None]] = None, num_ctx: Optional[int] = None,
                    rounds: Optional[list[dict]] = None) -> str:
    """Non-streaming chat completion, with an optional bounded tool-calling
    loop (David's ask 2026-08-31 — see module docstring). Without `tools`,
    behaves exactly as before this change.

    on_usage (David's ask 2026-09-01, per-model token usage on Home) fires
    with the raw `usage` object from any response that includes one —
    best-effort, since not every OpenAI-compatible server returns usage on
    every call; a server that never does simply never reports usage, same
    honest-degradation posture as the tools fallback above.

    num_ctx: see module docstring — applied by _post_chat().
    rounds: see module docstring."""
    working_messages = list(messages)
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for _ in range(MAX_TOOL_ROUNDS if tools else 1):
            body = {"model": model, "messages": _request_messages(base_url, model, working_messages)}
            if tools:
                body["tools"] = tools
            if num_ctx:
                body["num_ctx"] = num_ctx
            try:
                data = await _post_chat(client, base_url, api_key, body)
            except httpx.HTTPStatusError as e:
                if tools and e.response.status_code in (400, 422):
                    # This endpoint doesn't understand `tools` at all — retry
                    # once, plain, rather than failing the turn outright.
                    retry_body = {"model": model, "messages": _request_messages(base_url, model, _without_tool_rounds(working_messages))}
                    if num_ctx:
                        retry_body["num_ctx"] = num_ctx
                    data = await _post_chat(client, base_url, api_key, retry_body)
                    if on_usage and data.get("usage"):
                        on_usage(data["usage"])
                    return data["choices"][0]["message"]["content"]
                raise

            if on_usage and data.get("usage"):
                on_usage(data["usage"])
            message = data["choices"][0]["message"]
            tool_calls = message.get("tool_calls")
            if not tool_calls and tool_executor:
                rescued = _extract_fake_tool_call(message.get("content"), tools)
                if rescued:
                    tool_calls = [rescued]
                    # Rewrite so the appended history has a real tool_calls
                    # field instead of the raw hallucinated JSON text — some
                    # servers reject an assistant message followed by tool
                    # messages when it doesn't actually claim to have called one.
                    message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
            if not tool_calls or not tool_executor:
                return message.get("content") or ""

            _record(working_messages, rounds, message)
            for call in tool_calls:
                fn = call["function"]
                args = _parse_tool_arguments(fn.get("arguments"))
                result = await tool_executor(fn["name"], args)
                _record(working_messages, rounds, {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })
        # Ran out of rounds without a final answer — return whatever text
        # came back on the last response rather than raising.
        return message.get("content") or "(no response after tool calls)"


async def run_turn_stream(base_url: str, model: str, api_key: Optional[str], messages: list[dict],
                           tools: Optional[list[dict]] = None, tool_executor: Optional[Callable[[str, dict], Awaitable[str]]] = None,
                           on_usage: Optional[Callable[[dict], None]] = None, num_ctx: Optional[int] = None,
                           rounds: Optional[list[dict]] = None) -> AsyncIterator[str]:
    """Streaming variant. Tool-calling rounds (if any) are resolved
    non-streamed first — a tool call has no incremental text of its own to
    stream — then only the final round streams token-by-token, same
    real-time feel as before for the common no-tool-call case.

    on_usage, rounds: see run_turn's docstring.
    num_ctx: see module docstring. The plain (no-tools) branch below is SSE-
    based and can't go through _post_chat()/chat_capped() — for a detected
    capped-Ollama endpoint it instead falls back to one non-streamed
    chat_capped() call and yields the whole reply at once (correctness over
    token-by-token smoothness for that specific case)."""
    if not tools:
        if num_ctx and ollama_client.is_ollama_url(base_url):
            data = await ollama_client.chat_capped(model, messages, num_ctx, base_url=base_url)
            if on_usage and data.get("usage"):
                on_usage(data["usage"])
            content = data["choices"][0]["message"].get("content") or ""
            if content:
                yield content
            return

        stream_body = {"model": model, "messages": _request_messages(base_url, model, messages), "stream": True,
                       "stream_options": {"include_usage": True}}
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            async with client.stream(
                "POST", f"{base_url}/chat/completions", headers=_headers(api_key), json=stream_body,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    if on_usage and chunk.get("usage"):
                        on_usage(chunk["usage"])
                    choices = chunk.get("choices") or []
                    delta = choices[0]["delta"].get("content") if choices else None
                    if delta:
                        yield delta
        return

    working_messages = list(messages)
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for round_num in range(MAX_TOOL_ROUNDS):
            body = {"model": model, "messages": _request_messages(base_url, model, working_messages), "tools": tools}
            if num_ctx:
                body["num_ctx"] = num_ctx
            try:
                data = await _post_chat(client, base_url, api_key, body)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (400, 422):
                    # Fall back to a plain streamed call with no tools.
                    async for chunk in run_turn_stream(base_url, model, api_key, _without_tool_rounds(working_messages),
                                                       tools=None, on_usage=on_usage, num_ctx=num_ctx):
                        yield chunk
                    return
                raise

            if on_usage and data.get("usage"):
                on_usage(data["usage"])
            message = data["choices"][0]["message"]
            tool_calls = message.get("tool_calls")
            if not tool_calls and tool_executor:
                rescued = _extract_fake_tool_call(message.get("content"), tools)
                if rescued:
                    tool_calls = [rescued]
                    message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
            if not tool_calls or not tool_executor:
                content = message.get("content") or ""
                if content:
                    yield content
                return

            _record(working_messages, rounds, message)
            for call in tool_calls:
                fn = call["function"]
                args = _parse_tool_arguments(fn.get("arguments"))
                result = await tool_executor(fn["name"], args)
                _record(working_messages, rounds, {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })
